import time
import numpy as np
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool
from trajectory_msgs.msg import JointTrajectory
from diagnostic_msgs.msg import DiagnosticStatus
from ament_index_python.packages import get_package_share_directory
from pathlib import Path
from so101_core.config import resolve
from so101_core.hardware import SO101Bus, MockFollower, MujocoFollower
from so101_core.control import reorder, InvalidAction, follower_goal
from so101_core.timing import MeasuredVelocity, stamp_ns
from so101_core.lerobot_compat import bus_options
from so101_core.ros_utils import (SystemNode, STATE_QOS, COMMAND_QOS, joint_state,
                                 trajectory, run)


class LeaderNode(SystemNode):
    def __init__(self):
        super().__init__("so101_leader_driver")
        self.declare_parameter("backend", "mock")
        self.backend = self.get_parameter("backend").value
        spec = self.config["robot"]
        self.bus = None
        if self.backend == "real":
            self.bus = SO101Bus(spec["leader"]["port"], resolve(spec, spec["leader"]["calibration"]),
                                 self.joints, spec["leader"]["gripper"],
                                 motors=spec["leader"]["motors"], baudrate=spec["leader"]["baudrate"],
                                 timeout_s=spec["leader"]["serial_timeout_s"], **bus_options(spec, "leader"))
        self.started = time.monotonic()
        self.velocity = MeasuredVelocity()
        self.pub = self.create_publisher(JointState, "/leader/joint_states", STATE_QOS)
        self.create_timer(1/spec["update_rate_hz"], self.tick)

    def tick(self):
        try:
            q = self.bus.read() if self.bus else np.array([
                0.2*np.sin(time.monotonic()-self.started)]*5+[0.6])
            msg = joint_state(self, ["leader_"+j for j in self.joints], q)
            msg.velocity = self.velocity.estimate(q, stamp_ns(msg.header.stamp))
            msg.effort = [float('nan')]*len(self.joints)
            self.pub.publish(msg)
            self.diagnostic(DiagnosticStatus.OK, "Leader connected", backend=self.backend)
        except Exception as error:
            self.diagnostic(DiagnosticStatus.ERROR, "Leader read failed: "+str(error))

    def destroy_node(self):
        if self.bus:
            self.bus.close()
        super().destroy_node()


class FollowerNode(SystemNode):
    def __init__(self):
        super().__init__("so101_follower_driver")
        self.declare_parameter("backend", "mock")
        self.backend = self.get_parameter("backend").value
        robot = self.config["robot"]
        self.spec = robot["follower"]
        if self.backend == "real":
            if not robot["hardware_qualified"]:
                raise ValueError("Measure limits/E-stop and set robot.hardware_qualified before real runtime")
            self.bus = SO101Bus(self.spec["port"], resolve(robot, self.spec["calibration"]),
                                 self.joints, self.spec["gripper"], motors=self.spec["motors"],
                                 baudrate=self.spec["baudrate"], timeout_s=self.spec["serial_timeout_s"],
                                 **bus_options(robot, "follower"))
        elif self.backend == "mujoco":
            model = Path(get_package_share_directory("so101_description"))/"mujoco/so101.xml"
            self.bus = MujocoFollower(self.joints, model, robot["update_rate_hz"])
        elif self.backend == "mock":
            self.bus = MockFollower(self.joints)
        else:
            raise ValueError("Unknown follower backend")
        self.bus.enable(False)
        self.enabled = False
        self.requested_enable, self.enable_received = False, 0.0
        self.command, self.command_received = None, 0.0
        self.last_diagnostics = 0.0
        self.velocity = MeasuredVelocity()
        self.state_pub = self.create_publisher(JointState, "/hardware/follower/state", STATE_QOS)
        self.applied_pub = self.create_publisher(JointTrajectory, "/follower/applied_action", COMMAND_QOS)
        self.create_subscription(JointState, "/hardware/follower/command", self.receive, COMMAND_QOS)
        self.create_subscription(Bool, "/hardware/motor_enable", self.enable, COMMAND_QOS)
        self.create_timer(1/robot["update_rate_hz"], self.tick)

    def receive(self, msg):
        try:
            if not self.fresh_header(msg.header, self.spec["command_timeout_s"]):
                raise InvalidAction("Stale hardware command")
            self.command = reorder(msg.name, msg.position, self.joints)
            self.command_received = time.monotonic()
        except InvalidAction as error:
            self.requested_enable = False
            self.command = None
            self.bus.enable(False)
            self.enabled = False
            self.diagnostic(DiagnosticStatus.ERROR, str(error))

    def enable(self, msg):
        self.requested_enable, self.enable_received = msg.data, time.monotonic()
        if not msg.data and self.enabled:
            self.bus.enable(False)
            self.enabled = False
            self.command = None
            self.command_received = 0.0

    def tick(self):
        try:
            now = time.monotonic()
            q = self.bus.read()
            # Actual measured servo state, never controller setpoints.
            msg = joint_state(self, self.joints, q)
            msg.velocity = self.velocity.estimate(q, stamp_ns(msg.header.stamp))
            msg.effort = [float('nan')]*len(self.joints)
            self.state_pub.publish(msg)
            fresh = (now-self.enable_received <= self.spec["command_timeout_s"] and
                     now-self.command_received <= self.spec["command_timeout_s"])
            allowed = self.requested_enable and fresh and self.command is not None
            if allowed:
                robot = self.config["robot"]
                goal = follower_goal(robot, self.command, q, getattr(self.bus, "units", None))
                if not self.enabled:
                    self.bus.write(q)
                    self.bus.enable(True)
                    self.enabled = True
                self.bus.write(goal)
                # Report the quantized/clipped servo target, not an earlier command.
                applied = self.bus.to_si(self.bus.from_si(goal)) if self.backend == "real" else goal
                self.applied_pub.publish(trajectory(self, self.joints, applied))
            elif self.enabled:
                self.bus.enable(False)
                self.enabled = False
                if self.requested_enable:
                    self.diagnostic(DiagnosticStatus.ERROR, "Hardware command/enable watchdog")
            interval = 2.0 if self.backend == "real" else 1.0
            if now-self.last_diagnostics >= interval:
                self.last_diagnostics = now
                measurements = self.bus.diagnostics() if self.backend == "real" else {}
                status = self.bus.consume_status() if self.backend == "real" else {}
                overload = status.get("gripper") == 0x20
                if self.backend == "real":
                    measurements["gripper_load_percent"] = abs(measurements["load"]["gripper"])/10
                measurements["gripper_position_rad"] = float(q[self.joints.index("gripper")])
                if allowed:
                    measurements["gripper_target_rad"] = float(goal[self.joints.index("gripper")])
                self.diagnostic(
                    DiagnosticStatus.WARN if overload else DiagnosticStatus.OK,
                    "Gripper overload reported (0x20); arm control continues" if overload else "Follower connected",
                    torque_enabled=self.enabled, backend=self.backend,
                    servo_status={name: f"0x{flags:02x}" for name, flags in status.items()},
                    gripper_overload_reported=overload, **measurements)
        except Exception as error:
            try:
                self.bus.enable(False)
            except Exception:
                pass
            self.enabled = False
            self.requested_enable = False
            self.diagnostic(DiagnosticStatus.ERROR, "Follower I/O failed: "+str(error))

    def destroy_node(self):
        self.bus.close()
        super().destroy_node()


def leader_main():
    run(LeaderNode)


def follower_main():
    run(FollowerNode)

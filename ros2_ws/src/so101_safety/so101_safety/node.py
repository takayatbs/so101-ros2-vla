import time
import numpy as np
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectory
from std_msgs.msg import Bool, String, Float64MultiArray
from std_srvs.srv import Trigger
from diagnostic_msgs.msg import DiagnosticStatus, DiagnosticArray
from so101_msgs.srv import SetControlMode
from so101_core.control import Mode, ModeMachine, make_control_filter, InvalidAction, reorder
from so101_core.lerobot_compat import control_profile
from so101_core.ros_utils import (SystemNode, STATE_QOS, COMMAND_QOS, LATCHED_QOS,
                                 trajectory, trajectory_values, run)


class SafetyNode(SystemNode):
    """Command arbiter and safety gate; sole publisher to the controller."""
    def __init__(self):
        super().__init__("safety_node")
        self.spec = self.config["safety"]
        self.machine = ModeMachine()
        self.direct = control_profile(self.config["robot"]) == "lerobot"
        self.filter = make_control_filter(self.config["robot"], self.spec)
        self.state, self.state_received = None, 0.0
        self.sources = {}
        self.physical_estop = False
        self.policy_ready = False
        self.last_tick = time.monotonic()
        self.last_stop_diagnostic = 0.0
        self.mode_pub = self.create_publisher(String, "/control_mode", LATCHED_QOS)
        self.enable_pub = self.create_publisher(Bool, "/hardware/motor_enable", COMMAND_QOS)
        self.selected_pub = self.create_publisher(JointTrajectory, "/selected_action", COMMAND_QOS)
        self.safe_pub = self.create_publisher(JointTrajectory, "/safe_action", COMMAND_QOS)
        self.input_pub = self.create_publisher(JointTrajectory, "/controller_input", COMMAND_QOS)
        self.controller_pub = self.create_publisher(
            Float64MultiArray if self.direct else JointTrajectory,
            "/forward_position_controller/commands" if self.direct else "/joint_trajectory_controller/joint_trajectory", COMMAND_QOS)
        self.create_subscription(JointState, "/hardware/follower/state", self.receive_state, STATE_QOS)
        self.create_subscription(Bool, "/physical_estop", self.receive_estop, COMMAND_QOS)
        self.create_subscription(Bool, "/policy/ready", lambda msg: setattr(self, "policy_ready", msg.data), LATCHED_QOS)
        self.create_subscription(DiagnosticArray, "/diagnostics", self.receive_diagnostics, 10)
        for mode, topic in [(Mode.TELEOP, "/teleop/action"), (Mode.AUTONOMOUS, "/policy/action"),
                             (Mode.MOVEIT, "/moveit/action")]:
            self.create_subscription(JointTrajectory, topic, lambda msg, m=mode: self.receive(m, msg), COMMAND_QOS)
        self.create_service(SetControlMode, "/set_control_mode", self.set_mode)
        self.create_service(Trigger, "/software_estop", self.estop)
        self.create_service(Trigger, "/reset_fault", self.reset)
        self.create_timer(1/self.spec["rate_hz"], self.tick)
        self.publish_mode()

    def publish_mode(self):
        self.mode_pub.publish(String(data=self.machine.mode.value))

    def halt(self, reason, estop=False):
        self.machine.stop(reason, estop)
        self.get_logger().error(self.machine.mode.value + ": " + reason)
        self.sources.clear()
        self.enable_pub.publish(Bool(data=False))
        self.publish_mode()
        self.diagnostic(DiagnosticStatus.ERROR, reason)
        self.last_stop_diagnostic = time.monotonic()

    def receive_diagnostics(self, msg):
        if self.machine.mode not in (Mode.IDLE, Mode.ESTOP, Mode.FAULT):
            for status in msg.status:
                if status.name == "so101_follower_driver" and status.level >= DiagnosticStatus.ERROR:
                    self.halt("Follower driver: "+status.message)

    def receive_estop(self, msg):
        self.physical_estop = msg.data
        if msg.data:
            self.halt("Physical E-stop input", estop=True)

    def receive_state(self, msg):
        try:
            if not self.fresh_header(msg.header, self.spec["state_timeout_s"]):
                return
            self.state = reorder(msg.name, msg.position, self.joints)
            self.state_received = time.monotonic()
        except InvalidAction as error:
            self.halt(str(error))

    def receive(self, mode, msg):
        if mode != self.machine.mode:
            return
        try:
            if not self.fresh_header(msg.header, self.spec["max_header_age_s"]):
                raise InvalidAction("Stale/future-dated command header")
            values = trajectory_values(msg, self.joints)
            self.sources[mode] = time.monotonic(), values, msg
        except InvalidAction as error:
            self.halt(str(error))

    def set_mode(self, request, response):
        try:
            target = Mode(request.mode.upper())
            if target != Mode.IDLE:
                if self.physical_estop or self.state is None or time.monotonic()-self.state_received > self.spec["state_timeout_s"]:
                    raise ValueError("Fresh follower state and released E-stop are required")
                if target == Mode.AUTONOMOUS and self.machine.mode == Mode.IDLE and not self.policy_ready:
                    raise ValueError("Policy not ready: remain in IDLE until /policy/ready is true")
                self.filter.reset(self.state)
            self.machine.set(target)
            self.sources.clear()
            self.enable_pub.publish(Bool(data=False))
            self.last_tick = time.monotonic()
            response.success, response.message = True, self.machine.mode.value
            self.publish_mode()
        except (ValueError, InvalidAction) as error:
            response.success, response.message = False, str(error)
        return response

    def estop(self, request, response):
        self.halt("Software E-stop", estop=True)
        response.success, response.message = True, "ESTOP latched; motor enable disabled"
        return response

    def reset(self, request, response):
        if self.physical_estop or self.state is None or time.monotonic()-self.state_received > self.spec["state_timeout_s"]:
            response.success, response.message = False, "Release E-stop and restore follower feedback"
        else:
            self.machine.reset()
            self.sources.clear()
            self.enable_pub.publish(Bool(data=False))
            self.publish_mode()
            response.success, response.message = True, "IDLE; explicit mode selection required"
        return response

    def tick(self):
        now = time.monotonic()
        dt, self.last_tick = now-self.last_tick, now
        if self.machine.mode in (Mode.IDLE, Mode.ESTOP, Mode.FAULT):
            self.enable_pub.publish(Bool(data=False))
            # Keep the cause visible to tools connected after the fault.
            if self.machine.mode != Mode.IDLE and now-self.last_stop_diagnostic >= 1.0:
                self.diagnostic(DiagnosticStatus.ERROR, self.machine.reason, mode=self.machine.mode.value)
                self.last_stop_diagnostic = now
            return
        if dt > self.spec["max_control_gap_s"]:
            self.halt("Safety control deadline exceeded")
            return
        if now-self.state_received > self.spec["state_timeout_s"]:
            self.halt("Follower feedback watchdog")
            return
        source = self.sources.get(self.machine.mode)
        if source is None:
            # Mode changes clear prior messages. Allow a bounded startup grace.
            if not hasattr(self, "waiting_since"):
                self.waiting_since = now
            if now-self.waiting_since > self.spec["command_timeout_s"]:
                self.halt("Command source did not start")
            self.enable_pub.publish(Bool(data=False))
            return
        if hasattr(self, "waiting_since"):
            del self.waiting_since
        if now-source[0] > self.spec["command_timeout_s"]:
            self.halt("Selected command source watchdog")
            return
        try:
            safe = self.filter.step(source[1], min(dt, 1/self.spec["rate_hz"]))
            if not np.isfinite(safe).all():
                raise InvalidAction("Nonfinite filtered command")
            msg = trajectory(self, self.joints, safe, self.spec["trajectory_duration_s"])
            self.selected_pub.publish(source[2])
            self.safe_pub.publish(msg)
            self.input_pub.publish(msg)
            self.controller_pub.publish(Float64MultiArray(data=safe.tolist()) if self.direct else msg)
            self.enable_pub.publish(Bool(data=True))
            self.diagnostic(DiagnosticStatus.OK, "Safety active", mode=self.machine.mode.value,
                            command_age_s=now-source[0], actual_rate_hz=1/max(dt, 1e-6))
        except InvalidAction as error:
            self.halt(str(error))


def main():
    run(SafetyNode)

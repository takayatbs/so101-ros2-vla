import time
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectory
from diagnostic_msgs.msg import DiagnosticStatus
from so101_core.control import map_leader, InvalidAction
from so101_core.lerobot_compat import control_profile
from so101_core.ros_utils import SystemNode, STATE_QOS, COMMAND_QOS, trajectory, run


class TeleopNode(SystemNode):
    def __init__(self):
        super().__init__("so101_teleop")
        self.spec = self.config["teleop"]
        self.latest = None
        self.pub = self.create_publisher(JointTrajectory, "/teleop/action", COMMAND_QOS)
        self.create_subscription(JointState, "/leader/joint_states", self.receive, STATE_QOS)
        self.create_timer(1/self.spec["rate_hz"], self.tick)

    def receive(self, msg):
        if not self.fresh_header(msg.header, self.spec["timeout_s"]):
            return
        try:
            action = map_leader(msg.name, msg.position, self.joints, self.spec["joint_mapping"],
                                self.config["robot"]["limits"] if control_profile(self.config["robot"]) == "bounded" else None)
            self.latest = time.monotonic(), action
        except InvalidAction as error:
            self.latest = None
            self.diagnostic(DiagnosticStatus.ERROR, str(error))

    def tick(self):
        if self.latest is None or time.monotonic()-self.latest[0] > self.spec["timeout_s"]:
            self.diagnostic(DiagnosticStatus.STALE, "Leader message timeout")
            return
        self.pub.publish(trajectory(self, self.joints, self.latest[1]))
        self.diagnostic(DiagnosticStatus.OK, "Leader messages arriving", target_hz=self.spec["rate_hz"])


def main():
    run(TeleopNode)

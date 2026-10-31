import time
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, qos_profile_sensor_data
from builtin_interfaces.msg import Duration
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from .config import load_system
from .control import InvalidAction, reorder

COMMAND_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)
LATCHED_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)
STATE_QOS = qos_profile_sensor_data


def seconds(stamp):
    return stamp.sec+stamp.nanosec/1e9


def trajectory(node, joints, positions, duration=0.02):
    msg = JointTrajectory()
    msg.header.stamp = node.get_clock().now().to_msg()
    msg.joint_names = list(joints)
    point = JointTrajectoryPoint()
    point.positions = [float(v) for v in positions]
    point.time_from_start = Duration(sec=int(duration), nanosec=int((duration%1)*1e9))
    msg.points = [point]
    return msg


def trajectory_values(msg, joints):
    if len(msg.points) != 1:
        raise InvalidAction("Streaming action requires exactly one point")
    return reorder(msg.joint_names, msg.points[0].positions, joints)


def joint_state(node, joints, values):
    msg = JointState()
    msg.header.stamp = node.get_clock().now().to_msg()
    msg.name, msg.position = list(joints), [float(v) for v in values]
    return msg


class SystemNode(Node):
    def __init__(self, name):
        super().__init__(name)
        self.declare_parameter("config", "/workspace/config/runtime/system.yaml")
        self.config = load_system(self.get_parameter("config").value)
        self.joints = self.config["robot"]["joints"]
        self.diag_pub = self.create_publisher(DiagnosticArray, "/diagnostics", 10)
        self._last_diag = 0.0

    def fresh_header(self, header, age=0.25):
        delta = self.get_clock().now().nanoseconds/1e9-seconds(header.stamp)
        return -0.05 <= delta <= age

    def diagnostic(self, level, message, **values):
        now = time.monotonic()
        if level == DiagnosticStatus.OK and now-self._last_diag < 1:
            return
        self._last_diag = now
        status = DiagnosticStatus(level=level, name=self.get_name(), hardware_id="so101",
                                  message=message,
                                  values=[KeyValue(key=k, value=str(v)) for k, v in values.items()])
        msg = DiagnosticArray(status=[status])
        msg.header.stamp = self.get_clock().now().to_msg()
        self.diag_pub.publish(msg)


def run(node_type):
    rclpy.init()
    node = None
    try:
        node = node_type()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

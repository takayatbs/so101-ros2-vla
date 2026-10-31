"""AUTONOMOUS must wait for a completed, command-free IDLE warm-up."""
import os
import time
import pytest

rclpy = pytest.importorskip('rclpy')
from rclpy.executors import SingleThreadedExecutor
from std_msgs.msg import Bool
from sensor_msgs.msg import JointState
from so101_msgs.srv import SetControlMode
from so101_safety.node import SafetyNode
from so101_core.control import Mode
from so101_core.ros_utils import STATE_QOS, LATCHED_QOS, joint_state

pytestmark = pytest.mark.ros


def test_autonomous_is_rejected_until_policy_ready():
    os.environ['ROS_DOMAIN_ID'] = str(100+os.getpid()%100)
    rclpy.init()
    safety, peer = SafetyNode(), rclpy.create_node('policy_readiness_test')
    executor = SingleThreadedExecutor()
    executor.add_node(safety)
    executor.add_node(peer)
    states = peer.create_publisher(JointState, '/hardware/follower/state', STATE_QOS)
    readiness = peer.create_publisher(Bool, '/policy/ready', LATCHED_QOS)
    client = peer.create_client(SetControlMode, '/set_control_mode')

    def spin_until(predicate, ready=False):
        deadline = time.monotonic()+8
        while not predicate() and time.monotonic() < deadline:
            states.publish(joint_state(peer, safety.joints, [0.0]*5+[0.5]))
            readiness.publish(Bool(data=ready))
            executor.spin_once(timeout_sec=0.01)
        assert predicate()

    def mode(value, ready):
        future = client.call_async(SetControlMode.Request(mode=value))
        spin_until(future.done, ready)
        return future.result()

    try:
        spin_until(lambda: safety.state is not None)
        rejected = mode('AUTONOMOUS', False)
        assert not rejected.success and 'Policy not ready' in rejected.message
        assert safety.machine.mode == Mode.IDLE
        spin_until(lambda: safety.policy_ready, True)
        assert mode('AUTONOMOUS', True).success
        assert mode('IDLE', True).success
    finally:
        executor.shutdown()
        safety.destroy_node()
        peer.destroy_node()
        rclpy.shutdown()

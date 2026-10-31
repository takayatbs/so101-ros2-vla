"""Grasping keeps TELEOP and MCAP alive; other faults still disable the arm.

Runs real ROS driver/safety/recorder nodes on an isolated DDS domain. The
serial adapter and controller forwarding are simulated; no hardware is opened.
"""
import json
import os
from pathlib import Path
import shutil
import time

import numpy as np
import pytest

rclpy = pytest.importorskip('rclpy')
from rclpy.executors import SingleThreadedExecutor
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
from sensor_msgs.msg import Image, JointState
from std_msgs.msg import Float64MultiArray
from so101_msgs.srv import SetControlMode, StartEpisode, StopEpisode
from so101_core.control import Mode
from so101_core.feetech import ServoStatusError
from so101_core.ros_utils import COMMAND_QOS, STATE_QOS, joint_state, trajectory
from so101_hardware import drivers
from so101_safety.node import SafetyNode
from so101_dataset.recorder import RecorderNode
from so101_dataset.episode import EpisodeNode

pytestmark = pytest.mark.ros


@pytest.mark.parametrize('fatal', [(6, 0x21), (2, 0x20)])
def test_grasp_warning_keeps_control_and_recording_active_then_fatal_stops(tmp_path, monkeypatch, fatal):
    folder = tmp_path/'config'
    shutil.copytree(Path(__file__).resolve().parents[1]/'config', folder)
    monkeypatch.setenv('ROS_DOMAIN_ID', str(100+os.getpid()%100))

    class GraspBus:
        def __init__(self, *args, **kwargs):
            self.overload, self.fatal, self.enabled = False, None, False
            self.writes = []
        def enable(self, value): self.enabled = value
        def read(self):
            if self.fatal: raise ServoStatusError(*self.fatal)
            # An object keeps the gripper partly open despite a closed target.
            return np.array([0.0]*5+[0.5])
        def write(self, values): self.writes.append(list(values))
        def from_si(self, values): return values
        def to_si(self, values): return values
        def diagnostics(self): return {'load': {'gripper': 300 if self.overload else 0}}
        def consume_status(self): return {'gripper': 0x20} if self.overload else {}
        def close(self): self.enabled = False

    fake = GraspBus()
    monkeypatch.setattr(drivers, 'SO101Bus', lambda *a, **kw: fake)
    rclpy.init(args=['--ros-args', '-p', f'config:={folder}/runtime/system.yaml', '-p', 'backend:=real'])
    follower, safety, recorder, episode = drivers.FollowerNode(), SafetyNode(), RecorderNode(), EpisodeNode()
    peer = rclpy.create_node('grasp_test_peer')
    executor = SingleThreadedExecutor()
    nodes = [follower, safety, recorder, episode, peer]
    for node in nodes: executor.add_node(node)
    commands = peer.create_publisher(JointState, '/hardware/follower/command', COMMAND_QOS)
    actions = peer.create_publisher(type(trajectory(peer, follower.joints, [0.0]*6)), '/teleop/action', COMMAND_QOS)
    peer.create_subscription(Float64MultiArray, '/forward_position_controller/commands',
                             lambda m: commands.publish(joint_state(peer, follower.joints, m.data)), COMMAND_QOS)
    images = peer.create_publisher(Image, '/camera/top/image_raw', STATE_QOS)
    diagnostics = []
    peer.create_subscription(DiagnosticArray, '/diagnostics',
                             lambda m: diagnostics.extend(s for s in m.status if s.name == 'so101_follower_driver'), 10)
    def publish():
        actions.publish(trajectory(peer, follower.joints, [0.0]*6))
        msg = Image(height=3, width=4, encoding='rgb8', step=12, data=bytes(36))
        msg.header.stamp = peer.get_clock().now().to_msg()
        images.publish(msg)
    peer.create_timer(.02, publish)

    def spin_until(predicate, timeout=8):
        end = time.monotonic()+timeout
        while not predicate() and time.monotonic()<end: executor.spin_once(timeout_sec=.01)
        assert predicate(), safety.machine.reason
    def call(kind, name, **values):
        client = peer.create_client(kind, name)
        assert client.wait_for_service(timeout_sec=3)
        future = client.call_async(kind.Request(**values))
        spin_until(future.done)
        result = future.result()
        peer.destroy_client(client)
        return result

    try:
        spin_until(lambda: safety.state is not None)
        assert call(SetControlMode, '/set_control_mode', mode='TELEOP').success
        spin_until(lambda: follower.enabled and episode.mode == 'TELEOP')
        started = call(StartEpisode, '/start_episode', task_id='grasp eraser', operator_name='pytest')
        assert started.success, started.message
        fake.overload = True
        spin_until(lambda: any(s.level == DiagnosticStatus.WARN for s in diagnostics))
        deadline = time.monotonic()+2.5
        while time.monotonic()<deadline: executor.spin_once(timeout_sec=.01)
        assert safety.machine.mode == Mode.TELEOP and follower.enabled and fake.enabled
        assert recorder.writer is not None and episode.active is not None
        assert len(fake.writes)>60 and fake.writes[-1][-1] == 0.0
        warning = next(s for s in reversed(diagnostics) if s.level == DiagnosticStatus.WARN)
        values = {v.key:v.value for v in warning.values}
        assert values['gripper_load_percent'] == '30.0'
        assert values['gripper_position_rad'] == '0.5' and values['gripper_target_rad'] == '0.0'
        assert '0x20' in values['servo_status']
        stopped = call(StopEpisode, '/stop_episode', success=True, failure_reason='')
        assert stopped.success, stopped.message
        meta = json.loads((tmp_path/'data/bags'/started.episode_id/'episode.json').read_text())
        assert meta['status'] == 'complete' and meta['success']
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=meta['bag_uri'], storage_id='mcap'),
                    rosbag2_py.ConverterOptions('cdr', 'cdr'))
        seen_warning, images_after_warning = False, 0
        while reader.has_next():
            topic, data, _ = reader.read_next()
            if topic == '/diagnostics':
                msg = deserialize_message(data, DiagnosticArray)
                seen_warning |= any(s.name == 'so101_follower_driver' and s.level == DiagnosticStatus.WARN for s in msg.status)
            if seen_warning and topic == '/camera/top/image_raw': images_after_warning += 1
        assert seen_warning and images_after_warning>20
        # A mixed gripper alarm or overload on an arm joint still stops everything.
        fake.fatal = fatal
        spin_until(lambda: safety.machine.mode == Mode.FAULT and not follower.enabled and not fake.enabled)
        assert f'0x{fatal[1]:02x}' in safety.machine.reason
    finally:
        executor.shutdown()
        for node in reversed(nodes): node.destroy_node()
        rclpy.shutdown()

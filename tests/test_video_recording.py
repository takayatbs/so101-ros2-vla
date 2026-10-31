"""Real ROS/MCAP and NVENC, synthetic cameras only; no robot or USB access."""
import json
import gzip
import os
from pathlib import Path
import shutil
import time

import numpy as np
import pytest
import yaml

rclpy = pytest.importorskip('rclpy')
pytest.importorskip('av')
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import QoSProfile, ReliabilityPolicy
from foxglove_msgs.msg import CompressedVideo
from so101_msgs.srv import StartRecording
from so101_core.config import load_system, config_hash, calibration_hashes
from so101_core.converter import convert
from so101_core.dataset import SO101Dataset
from so101_core.ros_utils import STATE_QOS, COMMAND_QOS, joint_state, trajectory
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectory
from so101_dataset.recorder import RecorderNode
from so101_hardware.camera import CameraNode
from so101_policy.node import PolicyNode

pytestmark = pytest.mark.ros


@pytest.mark.parametrize('width,height,record_s', [
    (160, 120, 1.1), pytest.param(640, 480, 10., marks=pytest.mark.soak)])
def test_three_shared_streams_mid_gop_record_and_training_conversion(tmp_path, monkeypatch, width, height, record_s):
    source = Path(__file__).resolve().parents[1]
    folder = tmp_path/'config'
    shutil.copytree(source/'config', folder)
    # Keep geometry small for the regression suite. The standalone measured
    # validation uses three 640x480 streams at the requested 30 FPS.
    p = folder/'cameras/camera.640x480.h264.yaml'
    spec = yaml.safe_load(p.read_text())
    for camera in spec['cameras'].values(): camera.update(width=width, height=height)
    p.write_text(yaml.safe_dump(spec))
    p = folder/'observation/observation.yaml'
    spec = yaml.safe_load(p.read_text())
    for pre in spec['preprocessing'].values(): pre['resize'].update(width=width, height=height)
    p.write_text(yaml.safe_dump(spec))
    config_path = folder/'runtime/system.real30hz.640x480.h264.yaml'
    config = load_system(config_path)
    monkeypatch.setenv('ROS_DOMAIN_ID', str(100+os.getpid()%100))
    rclpy.init(args=['--ros-args', '-p', f'config:={config_path}', '-p', 'backend:=mock'])
    nodes = []
    executor = SingleThreadedExecutor()
    original = CameraNode.declare_parameter
    try:
        for name in ('top', 'side', 'arm'):
            with monkeypatch.context() as patch:
                def declare(self, key, default=None, *a, _name=name, **kw):
                    return original(self, key, _name if key == 'camera' else default, *a, **kw)
                patch.setattr(CameraNode, 'declare_parameter', declare)
                nodes.append(CameraNode())
        recorder, hold = RecorderNode(), PolicyNode()
        nodes.extend([recorder, hold])
        assert not hold.use_camera_observations
        peer = rclpy.create_node('shared_video_test_peer')
        nodes.append(peer)
        for node in nodes: executor.add_node(node)
        received = {n: [] for n in ('top', 'side', 'arm')}
        for name in received:
            peer.create_subscription(CompressedVideo, f'/camera/{name}/video',
                lambda msg, n=name: received[n].append(msg),
                QoSProfile(depth=30, reliability=ReliabilityPolicy.RELIABLE))
        states = peer.create_publisher(JointState, '/follower/joint_states', STATE_QOS)
        actions = peer.create_publisher(JointTrajectory, '/follower/applied_action', COMMAND_QOS)
        def publish_state():
            states.publish(joint_state(peer, config['robot']['joints'], [0.]*6))
            actions.publish(trajectory(peer, config['robot']['joints'], [.1]*6))
        peer.create_timer(1/60, publish_state)
        def spin_for(duration):
            end = time.monotonic()+duration
            while time.monotonic() < end: executor.spin_once(timeout_sec=.005)
        spin_for(.85)
        assert all(len(v) >= 10 for v in received.values())
        assert all(c.video.error is None for c in nodes[:3])
        assert all(c.pub.get_subscription_count() == 0 for c in nodes[:3])
        episode = tmp_path/'data/bags/synthetic_shared_video'
        start = peer.get_clock().now().nanoseconds/1e9
        response = recorder.start(StartRecording.Request(bag_uri=str(episode/'raw')), StartRecording.Response())
        assert response.success, response.message
        duplicate_start = recorder.start(StartRecording.Request(bag_uri=str(episode/'other')),StartRecording.Response())
        assert not duplicate_start.success and recorder.writer is not None
        spin_for(record_s)
        end = peer.get_clock().now().nanoseconds/1e9
        recorder.close_recording()
        manifest = json.loads((episode/'recording.json').read_text())
        assert manifest['clock']['hardware_trigger'] is False
        assert 'host acquisition' in manifest['camera_timestamp_source']
        assert manifest['config']['camera']['cameras']['top']['width'] == width
        with gzip.open(episode/'raw/message_index.jsonl.gz','rt') as index:
            identities = [json.loads(line) for line in index]
        assert all(item['episode_id'] == episode.name for item in identities)
        top_index = [item for item in identities if item['topic_name'] == '/camera/top/video']
        assert top_index[0]['source_timestamp_ns'] < round(start*1e9)
        assert top_index[0]['receive_timestamp_ns'] < round(start*1e9)
        assert all(item['write_call_timestamp_ns'] >= item['receive_timestamp_ns'] for item in identities)
        assert all(b['message_id']>a['message_id'] for a,b in zip(top_index,top_index[1:]))
        assert json.loads((episode/'recording_summary.json').read_text())['/camera/top/video']['observed_hz'] > 25
        (episode/'episode.json').write_text(json.dumps({
            'episode_id': episode.name, 'task_id': 'synthetic validation', 'success': True, 'status': 'complete',
            'start_time': start, 'end_time': end, 'robot_hash': config_hash(config['robot']),
            'calibration_sha256': calibration_hashes(config['robot'])}))
        output = tmp_path/'dataset'
        result = convert([episode], output, 'test/shared-video', config)
        dataset = SO101Dataset(output)
        assert dataset.info['format'] == 'so101_dataset_v2'
        assert not list(output.rglob('*.png'))
        assert len(list(output.rglob('*.mp4'))) == 2
        assert all(value == 'streamcopy' for value in result['video_operations'][episode.name].values())
        assert (output/'episodes/000000/review.html').is_file()
        assert json.loads((output/'episodes/000000/sync_report.json').read_text())['camera_host_sequence_gaps']['top']['source_sequence_available']
        assert result['episodes'][0]['frames'] >= int(record_s*30*.9)
        # All in-episode frames can be decoded, including the start mid-GOP.
        first, last = dataset[0], dataset[len(dataset)-1]
        for frame in (first, last):
            assert frame['images']['top'].shape == (height, width, 3)
            np.testing.assert_allclose(frame['images']['top'][0,0], [80,120,170], atol=5)
            np.testing.assert_allclose(frame['action'][0], [.1]*6)
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(episode/'raw'), storage_id='mcap'),
                    rosbag2_py.ConverterOptions('cdr','cdr'))
        topics = {t.name: t.type for t in reader.get_all_topics_and_types()}
        assert not any(t.endswith('image_raw') for t in topics)
        saved = {n: [] for n in received}
        while reader.has_next():
            topic, data, _ = reader.read_next()
            if topic.endswith('/video'):
                msg = deserialize_message(data, CompressedVideo)
                saved[topic.split('/')[2]].append(msg)
        for name, messages in saved.items():
            inside = [m for m in messages if start <= m.timestamp.sec+m.timestamp.nanosec/1e9 <= end]
            assert len(inside) >= int(record_s*30*.9)
            live = {(m.timestamp.sec, m.timestamp.nanosec): bytes(m.data) for m in received[name]}
            assert all(live[(m.timestamp.sec,m.timestamp.nanosec)] == bytes(m.data) for m in messages)
            assert messages[0].timestamp.sec+messages[0].timestamp.nanosec/1e9 < start
        (tmp_path/'validation.json').write_text(json.dumps({
            'synthetic_only': True, 'width': width, 'height': height, 'requested_fps': 30,
            'duration_s': end-start, 'dataset_frames': len(dataset),
            'mcap_bytes': sum(p.stat().st_size for p in (episode/'raw').glob('*.mcap')),
            'cameras': {name: {
                'recorded_frames_in_episode': sum(start <= m.timestamp.sec+m.timestamp.nanosec/1e9 <= end for m in messages),
                'stored_frames_including_preroll': len(messages),
                'encoded_payload_bytes': sum(len(m.data) for m in messages),
                'stream_identical_to_live': True,
                'raw_subscriber_count': nodes[i].pub.get_subscription_count(),
                'encoder_dropped_frames': nodes[i].video.dropped,
                'last_encode_ms': nodes[i].video.encode_ms
            } for i, (name, messages) in enumerate(saved.items())}
        }, indent=2))
    finally:
        for node in reversed(nodes):
            executor.remove_node(node)
            node.destroy_node()
        executor.shutdown()
        rclpy.shutdown()

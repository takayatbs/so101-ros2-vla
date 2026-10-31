"""Exercise real capture delivery with a fake USB device; never touch hardware."""
import json
from pathlib import Path
import queue
import shutil
import threading
import time

import numpy as np
import pytest
import yaml

rclpy = pytest.importorskip('rclpy')
from rclpy.executors import SingleThreadedExecutor
from std_msgs.msg import String
from so101_hardware.camera import CameraNode

pytestmark = pytest.mark.ros


class FakeCapture:
    def __init__(self):
        self.frames = queue.Queue()
        self.current = None
        self.released = False
        self.in_grab = False

    def set(self, *args):
        return True

    def isOpened(self):
        return True

    def grab(self):
        self.in_grab = True
        try:
            self.current = self.frames.get(timeout=.01)
            return True
        except queue.Empty:
            return False
        finally:
            self.in_grab = False

    def retrieve(self):
        if isinstance(self.current, Exception):
            raise self.current
        return True, np.full((48, 64, 3), self.current, dtype=np.uint8)

    def release(self):
        assert not self.in_grab
        self.released = True


@pytest.fixture
def camera(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root/'config', tmp_path/'config')
    path = tmp_path/'config/cameras/camera.640x480.h264.yaml'
    spec = yaml.safe_load(path.read_text())
    spec['video']['enabled'] = False
    for value in spec['cameras'].values():
        value.update(width=64, height=48, warmup_s=0)
    path.write_text(yaml.safe_dump(spec))
    capture = FakeCapture()
    monkeypatch.setattr('so101_hardware.camera.cv2.VideoCapture', lambda *args: capture)
    monkeypatch.setenv('ROS_DOMAIN_ID', '211')
    rclpy.init(args=['--ros-args', '-p', 'config:='+str(tmp_path/'config/runtime/system.real30hz.640x480.h264.yaml'),
                    '-p', 'backend:=real'])
    node = CameraNode()
    peer = rclpy.create_node('camera_capture_test')
    executor = SingleThreadedExecutor()
    executor.add_node(node); executor.add_node(peer)
    received = []
    peer.create_subscription(String, '/camera/top/frame_metadata', lambda m: received.append(json.loads(m.data)), 30)
    def spin_until(predicate):
        deadline = time.monotonic()+3
        while not predicate() and time.monotonic() < deadline:
            executor.spin_once(timeout_sec=.01)
        assert predicate()
    spin_until(lambda: node.frame_meta_pub.get_subscription_count() > 0)
    try:
        yield node, capture, received, spin_until
    finally:
        executor.remove_node(node); executor.remove_node(peer)
        node.destroy_node(); peer.destroy_node(); executor.shutdown(); rclpy.shutdown()
        assert capture.released and not node.thread.is_alive()


def test_capture_arrival_wakes_executor_without_polling_and_keeps_source_time(camera):
    node, capture, received, spin_until = camera
    # Delivery must work even with the watchdog disabled: it is an arrival event,
    # not another periodic camera sampler.
    node.capture_watchdog.cancel()
    before = node.get_clock().now().nanoseconds
    capture.frames.put(17)
    spin_until(lambda: len(received) == 1)
    sample = received[0]
    assert sample['source_sequence'] == 1
    assert before <= sample['timestamp_ns'] <= sample['capture_ready_timestamp_ns']
    assert sample['capture_ready_timestamp_ns'] <= sample['publish_started_timestamp_ns'] <= sample['metadata_generated_timestamp_ns']
    assert sample['timestamp_source'] == 'host_after_grab_before_decode'
    assert sample['hardware_timestamp_ns'] is None
    node.tick()
    assert len(received) == 1


def test_slow_executor_keeps_only_latest_capture_and_reports_skips(camera):
    node, capture, received, spin_until = camera
    capture.frames.put(1)
    spin_until(lambda: len(received) == 1)
    for value in range(2, 11):
        capture.frames.put(value)
    deadline = time.monotonic()+2
    while (node.latest is None or node.latest[0] < 10) and time.monotonic() < deadline:
        time.sleep(.001)
    assert node.latest[0] == 10
    spin_until(lambda: len(received) == 2)
    assert [r['source_sequence'] for r in received] == [1, 10]
    assert node.dropped == 8


def test_capture_worker_error_is_visible_to_watchdog(camera, monkeypatch):
    node, capture, received, spin_until = camera
    diagnostics = []
    monkeypatch.setattr(node, 'diagnostic', lambda *args, **kwargs: diagnostics.append(args))
    capture.frames.put(RuntimeError('USB decode failed'))
    spin_until(lambda: any('USB decode failed' in d[1] for d in diagnostics))
    assert node.capture_error == 'USB decode failed'
    assert not node.thread.is_alive()


def test_watchdog_reports_stall_without_capture_arrivals(camera, monkeypatch):
    node, capture, received, spin_until = camera
    diagnostics = []
    monkeypatch.setattr(node, 'diagnostic', lambda *args, **kwargs: diagnostics.append(args))
    node.capture_started = time.monotonic()-node.config['camera']['timeout_s']-1
    node.check_capture()
    assert diagnostics[-1][1] == 'Camera frame timeout'

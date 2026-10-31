"""The live observation must use the same past-only selection as Dataset v2."""
from collections import deque
import os
from pathlib import Path

import numpy as np
import pytest
rclpy = pytest.importorskip('rclpy')
from builtin_interfaces.msg import Time
from sensor_msgs.msg import Image
from so101_policy.node import PolicyNode
from so101_core.ros_utils import joint_state

pytestmark = pytest.mark.ros


def test_live_observation_does_not_select_future_image_or_state(monkeypatch):
    monkeypatch.setenv('ROS_DOMAIN_ID',str(100+os.getpid()%100))
    config = Path(__file__).resolve().parents[1]/'config/runtime/system.real30hz.640x480.h264.yaml'
    rclpy.init(args=['--ros-args','-p',f'config:={config}','-p','policy_backend:=hold'])
    node = PolicyNode()
    try:
        node.use_camera_observations = True
        now = node.get_clock().now().nanoseconds
        def stamp(ns): return Time(sec=ns//10**9,nanosec=ns%10**9)
        for age,q in [(40_000_000,.1),(10_000_000,.9)]:
            msg = joint_state(node,node.joints,[q]*6)
            msg.header.stamp = stamp(now-age)
            node.receive_measured_state(msg)
        controller = joint_state(node,node.joints,[.7]*6)
        node.receive_controller_state(controller)
        assert node.state_source == '/hardware/follower/state'
        def image(name,age,value):
            msg = Image(height=4,width=4,encoding='rgb8',step=12,data=bytes([value])*48)
            msg.header.stamp = stamp(now-age)
            node.receive_image(name,msg)
        image('arm',43_000_000,50)
        image('top',30_000_000,20)
        image('arm',10_000_000,200)
        observation = node.observation()
        np.testing.assert_allclose(observation['observation.state'],[.1]*6)
        assert (observation['observation.images.arm'] == 50).all()
        assert (observation['observation.images.top'] == 20).all()
    finally:
        node.destroy_node()
        rclpy.shutdown()

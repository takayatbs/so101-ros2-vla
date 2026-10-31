#!/usr/bin/env python3
"""ROS 2 learning example: one simulated joint, without hardware access."""
import math
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64


class OneJointDemo(Node):
    """Move a simulated position toward a bounded command and stop on timeout."""

    def __init__(self):
        super().__init__('learning_joint_demo')
        self.position = 0.0
        self.target = 0.0
        self.last_command = None
        self.last_tick = time.monotonic()
        self.timed_out = False
        command_qos = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        state_qos = QoSProfile(
            depth=5, reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        self.publisher = self.create_publisher(
            JointState, '/learning/joint_states', state_qos,
        )
        self.subscription = self.create_subscription(
            Float64, '/learning/joint_command', self.on_command, command_qos,
        )
        self.timer = self.create_timer(1.0 / 30.0, self.on_tick)
        self.get_logger().info('Simulated joint only: no USB or motor access.')

    def on_command(self, message):
        if not math.isfinite(message.data):
            self.get_logger().warning('Rejected non-finite command.')
            return
        self.target = max(-0.5, min(0.5, message.data))
        self.last_command = time.monotonic()
        self.timed_out = False

    def on_tick(self):
        now = time.monotonic()
        # After a delayed callback, avoid jumping to catch up with elapsed time.
        dt = min(max(now - self.last_tick, 0.0), 0.1)
        self.last_tick = now
        fresh = self.last_command is not None and now - self.last_command <= 0.5
        if fresh:
            step = 0.2 * dt
            self.position += max(-step, min(step, self.target - self.position))
        elif self.last_command is not None and not self.timed_out:
            self.target = self.position
            self.timed_out = True
            self.get_logger().warning('Command timeout: hold simulated position.')
        state = JointState()
        state.header.stamp = self.get_clock().now().to_msg()
        state.name = ['learning_joint']
        state.position = [self.position]
        self.publisher.publish(state)


def main(args=None):
    rclpy.init(args=args)
    node = OneJointDemo()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()

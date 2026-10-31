#!/usr/bin/env python3
"""Smoke checks within the dev container; no physical hardware is opened."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def command(*args, timeout=20):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True).stdout.strip()


def window(name, timeout=20):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--name', name], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.splitlines()
        time.sleep(0.2)
    raise RuntimeError(f'No visible {name} window')


def main():
    assert os.getuid() != 0, 'GUI verification must run as a normal user'
    os_release = dict(line.rstrip().split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    assert os_release['VERSION_ID'].strip('"') == '24.04'
    assert os.environ.get('ROS_DISTRO') == 'jazzy'
    assert Path('/etc/so101/unified').exists(), 'Expected the unified GUI / SO-101 image'
    command('/usr/local/bin/physical-ai-healthcheck')
    result = {'uid': os.getuid(), 'os': os_release['PRETTY_NAME'].strip('"'),
              'ros': 'jazzy', 'desktop': 'MATE', 'architecture': command('dpkg', '--print-architecture'),
              'renderer': command('glxinfo', '-B'), 'ros_communication': False, 'visible_windows': {},
              'appearance': {
                  'gtk_theme': command('gsettings', 'get', 'org.mate.interface', 'gtk-theme'),
                  'icon_theme': command('gsettings', 'get', 'org.mate.interface', 'icon-theme'),
                  'font': command('gsettings', 'get', 'org.mate.interface', 'font-name'),
                  'window_theme': command('gsettings', 'get', 'org.mate.Marco.general', 'theme'),
                  'compositing': command('gsettings', 'get', 'org.mate.Marco.general', 'compositing-manager'),
              }}
    result['workspace'] = str(Path('/workspace').resolve())
    result['python'] = sys.executable
    result['robot_package'] = command('ros2', 'pkg', 'prefix', 'so101_bringup')
    result['robot_messages'] = command('ros2', 'interface', 'show', 'so101_msgs/srv/SetControlMode')
    for asset in ['/usr/share/themes/Yaru-MATE-light', '/usr/share/icons/Yaru-MATE-light',
                  '/usr/share/backgrounds/ubuntu-mate-common/Green-Wall-Logo.png']:
        assert Path(asset).exists(), 'Missing desktop appearance asset: '+asset
    # Nodes communicate through the same container's loopback; no robot Topics.
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import SingleThreadedExecutor
    from std_msgs.msg import String
    rclpy.init()
    sender, receiver = Node('gui_check_sender'), Node('gui_check_receiver')
    received = []
    pub = sender.create_publisher(String, '/gui_check/ping', 10)
    sub = receiver.create_subscription(String, '/gui_check/ping', lambda m: received.append(m.data), 10)
    executor = SingleThreadedExecutor()
    executor.add_node(sender)
    executor.add_node(receiver)
    try:
        deadline = time.monotonic()+8
        while time.monotonic() < deadline and 'gui-check' not in received:
            pub.publish(String(data='gui-check'))
            executor.spin_once(timeout_sec=0.1)
        assert 'gui-check' in received, 'ROS 2 pub/sub failed'
        result['ros_communication'] = True
    finally:
        executor.shutdown()
        sender.destroy_node()
        receiver.destroy_node()
        rclpy.shutdown()
    # Real X11 windows, not just installed binaries or a successful process fork.
    processes = []
    try:
        terminal = subprocess.Popen(['mate-terminal', '--disable-factory', '--title=Physical-AI-terminal-check',
                                     '--command=bash -lc "printf GUI-terminal-OK; sleep 30"'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        processes.append(terminal)
        result['visible_windows']['terminal'] = window('Physical-AI-terminal-check')
        for name, args in [
            ('rviz', ['ros2', 'run', 'rviz2', 'rviz2']),
            ('rqt', ['ros2', 'run', 'rqt_gui', 'rqt_gui']),
            ('turtlesim', ['ros2', 'run', 'turtlesim', 'turtlesim_node']),
        ]:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, start_new_session=True)
            processes.append(process)
            result['visible_windows'][name] = window(name)
            assert process.poll() is None, f'{name} exited unexpectedly'
        print(json.dumps(result, indent=2))
    finally:
        for process in reversed(processes):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


if __name__ == '__main__':
    main()

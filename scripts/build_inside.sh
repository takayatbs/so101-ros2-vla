#!/usr/bin/env bash
set -e
source /opt/ros/jazzy/setup.bash
cd /workspace/ros2_ws
/opt/venv/bin/python -m colcon build --symlink-install --cmake-args -DPython3_EXECUTABLE=/opt/venv/bin/python

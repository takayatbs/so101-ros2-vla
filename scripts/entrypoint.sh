#!/usr/bin/env bash
set -e
export RMW_FASTRTPS_PUBLICATION_MODE="${RMW_FASTRTPS_PUBLICATION_MODE:-ASYNCHRONOUS}"
source /opt/ros/jazzy/setup.bash
if [[ -f /workspace/ros2_ws/install/setup.bash ]]; then
  source /workspace/ros2_ws/install/setup.bash
fi
export PYTHONPATH="/workspace/ros2_ws/src/so101_core${PYTHONPATH:+:$PYTHONPATH}"
exec "$@"

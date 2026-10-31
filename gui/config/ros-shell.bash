# Sourced by both desktop terminals and the container entrypoint.
source /opt/ros/jazzy/setup.bash
export PATH="/opt/venv/bin:$PATH"
export RMW_FASTRTPS_PUBLICATION_MODE="${RMW_FASTRTPS_PUBLICATION_MODE:-ASYNCHRONOUS}"
if [[ -f /workspace/ros2_ws/install/setup.bash ]]; then
  source /workspace/ros2_ws/install/setup.bash
fi
export PYTHONPATH="/workspace/ros2_ws/src/so101_core${PYTHONPATH:+:$PYTHONPATH}"
# Compatibility with earlier manuals; plain ros2 is the normal GUI command.
so101_ros() { ros2 "$@"; }

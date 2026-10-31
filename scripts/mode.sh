#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
TASK_MODE="${1:?Usage: mode.sh IDLE|TELEOP|AUTONOMOUS|MOVEIT}"
case "$TASK_MODE" in IDLE|TELEOP|AUTONOMOUS|MOVEIT) ;; *) exit 2;; esac
container_exec ros2 service call /set_control_mode so101_msgs/srv/SetControlMode "{mode: $TASK_MODE}"

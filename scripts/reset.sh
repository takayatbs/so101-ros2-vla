#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
container_exec ros2 service call /reset_fault std_srvs/srv/Trigger '{}'

#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
container_start
# Dataset conversion is offline work; do not contend with runtime.
if container_exec flock -n -E 75 /workspace/data/.compute.lock python -m so101_core.converter "$@"; then
  exit 0
else
  TASK_CONVERT_STATUS=$?
  if [[ "$TASK_CONVERT_STATUS" == 75 ]]; then
    echo 'Conversion not started: Runtime or training is running. Stop it before converting; no output was created.' >&2
  fi
  exit "$TASK_CONVERT_STATUS"
fi

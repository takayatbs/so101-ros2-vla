#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
container_start
# Excludes a running runtime/training session using this project's shared lock.
if container_exec flock -n -E 75 /workspace/data/.compute.lock python /workspace/scripts/hardware_probe.py "$@"; then
  exit 0
else
  TASK_PROBE_STATUS=$?
  if [[ "$TASK_PROBE_STATUS" == 75 ]]; then
    echo 'Probe not started: Runtime or training is using the devices. Stop it before running probe.sh; no output file was created.' >&2
  fi
  exit "$TASK_PROBE_STATUS"
fi

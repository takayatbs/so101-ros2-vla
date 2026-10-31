#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
container_start
if container_exec flock -n -E 75 /workspace/data/.compute.lock bash -c '
  TASK_PYTHON=/workspace/data/envs/policies/bin/python
  if [[ ! -x "$TASK_PYTHON" ]]; then TASK_PYTHON=python; fi
  exec "$TASK_PYTHON" -m so101_core.methods_cli "$@"
' bash "$@"; then
  exit 0
else
  TASK_STATUS=$?
  if [[ "$TASK_STATUS" == 75 ]]; then
    echo 'Runtime/training is running. Stop it before exporting or training; no output was created.' >&2
  fi
  exit "$TASK_STATUS"
fi

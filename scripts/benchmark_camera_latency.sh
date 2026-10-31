#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
TASK_CAMERA_BENCH_ARGS=()
for TASK_CAMERA_BENCH_ARG in "$@"; do
  if [[ "$TASK_CAMERA_BENCH_ARG" == "$TASK_ROOT/"* ]]; then
    TASK_CAMERA_BENCH_ARGS+=("/workspace/${TASK_CAMERA_BENCH_ARG#"$TASK_ROOT/"}")
  else
    TASK_CAMERA_BENCH_ARGS+=("$TASK_CAMERA_BENCH_ARG")
  fi
done
container_start
if container_exec flock -n -E 75 /workspace/data/.compute.lock python /workspace/scripts/benchmark_camera_latency.py "${TASK_CAMERA_BENCH_ARGS[@]}"; then
  exit 0
else
  TASK_CAMERA_BENCH_STATUS=$?
  if [[ "$TASK_CAMERA_BENCH_STATUS" == 75 ]]; then
    echo 'Camera benchmark not started: Runtime or training is running. Stop it before measuring.' >&2
  fi
  exit "$TASK_CAMERA_BENCH_STATUS"
fi

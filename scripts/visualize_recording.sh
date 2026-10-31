#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
TASK_REVIEW_ARGS=()
for TASK_REVIEW_ARG in "$@"; do
  if [[ "$TASK_REVIEW_ARG" == "$TASK_ROOT/"* ]]; then
    TASK_REVIEW_ARGS+=("/workspace/${TASK_REVIEW_ARG#"$TASK_ROOT/"}")
  else
    TASK_REVIEW_ARGS+=("$TASK_REVIEW_ARG")
  fi
done
container_start
if container_exec flock -n -E 75 /workspace/data/.compute.lock python -m so101_core.recording_review "${TASK_REVIEW_ARGS[@]}"; then
  exit 0
else
  TASK_REVIEW_STATUS=$?
  if [[ "$TASK_REVIEW_STATUS" == 75 ]]; then
    echo 'Visualization not started: Runtime or training is running. Stop it before generating a review.' >&2
  fi
  exit "$TASK_REVIEW_STATUS"
fi

#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
container_start
# Interactive measurement needs a TTY; check/apply/help also work in scripts.
if [[ $SO101_NATIVE == 0 && -t 0 && -t 1 ]]; then
  "${COMPOSE[@]}" exec so101-dev /usr/local/bin/so101-entrypoint \
    flock -n -E 75 /workspace/data/.compute.lock python -m so101_core.calibration "$@"
else
  container_exec flock -n -E 75 /workspace/data/.compute.lock python -m so101_core.calibration "$@"
fi

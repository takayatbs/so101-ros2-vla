#!/usr/bin/env bash
# Preserve the focused terminal for interactive trial decisions.
source "$(dirname "$0")/common.sh"
if [[ "$SO101_NATIVE" == 1 ]]; then
  exec /usr/local/bin/so101-entrypoint python /workspace/scripts/benchmark.py "$@"
fi
container_start
if [[ -t 0 && -t 1 ]]; then
  exec "${COMPOSE[@]}" exec so101-dev /usr/local/bin/so101-entrypoint python /workspace/scripts/benchmark.py "$@"
fi
exec "${COMPOSE[@]}" exec -T so101-dev /usr/local/bin/so101-entrypoint python /workspace/scripts/benchmark.py "$@"

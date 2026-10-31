#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TASK_ROOT"
if [[ -f /etc/so101/unified ]]; then
  # Desktop terminals already run in the robot's environment. Never nest Docker.
  SO101_NATIVE=1
  container_start() { :; }
  container_exec() { /usr/local/bin/so101-entrypoint "$@"; }
  return 0
fi
SO101_NATIVE=0
COMPOSE=(docker compose --project-directory "$TASK_ROOT" --env-file "$TASK_ROOT/.env" -f "$TASK_ROOT/compose.yaml")
if [[ -f .env ]]; then
  TASK_GPU="$(sed -n 's/^GPU=//p' .env | tail -n 1)"
else
  TASK_GPU="${GPU:-0}"
fi
if [[ "${GPU:-$TASK_GPU}" == 1 ]]; then
  COMPOSE+=(-f compose.gpu.yaml)
fi
container_start() {
  "${COMPOSE[@]}" up -d
}
container_exec() { "${COMPOSE[@]}" exec -T so101-dev /usr/local/bin/so101-entrypoint "$@"; }

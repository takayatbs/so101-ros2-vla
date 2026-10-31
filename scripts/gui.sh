#!/usr/bin/env bash
# Host management for the single GUI / SO-101 container.
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${1:-}" == --gpu ]]; then export GPU=1; shift; fi
if [[ "${1:-}" == --cpu ]]; then export GPU=0; shift; fi
operation="${1:-help}"
if [[ $# -gt 0 ]]; then shift; fi
if [[ $operation == help || $operation == --help || $operation == -h ]]; then
  echo 'Usage: ./scripts/gui.sh [--gpu|--cpu] init|passwd|build|up|down|logs|ps|config|shell|exec|check|recreate-check|permissions'
  echo 'Host first: init, build, up.'
  echo 'Inside GUI: use teleop.sh / train.sh / deploy_policy.sh directly from /workspace.'
  exit 0
fi
if [[ -f /etc/so101/unified ]]; then
  case "$operation" in
    shell) exec /usr/local/bin/so101-entrypoint bash ;;
    exec) exec /usr/local/bin/so101-entrypoint "$@" ;;
    check) exec flock -n -E 75 /workspace/data/.compute.lock /usr/local/bin/physical-ai-verify ;;
    *) echo 'Run container management on the Linux host. GUI task scripts run directly inside /workspace.' >&2; exit 2 ;;
  esac
fi
case "$operation" in
  init) exec python3 "$TASK_ROOT/gui/scripts/init.py" "$@" ;;
  passwd) exec python3 "$TASK_ROOT/gui/scripts/init.py" --reset-password "$@" ;;
esac
if [[ ! -f "$TASK_ROOT/.env" || ! -f "$TASK_ROOT/gui/.secrets/vnc_password" ]]; then
  echo 'Run ./scripts/gui.sh init on the host first.' >&2; exit 1
fi
source "$TASK_ROOT/scripts/common.sh"
case "$operation" in
  build) exec "${COMPOSE[@]}" build "$@" ;;
  up) exec "${COMPOSE[@]}" up -d --wait "$@" ;;
  permissions) exec python3 "$TASK_ROOT/gui/scripts/permissions.py" "${COMPOSE[@]}" ;;
  down) exec "${COMPOSE[@]}" down "$@" ;;
  logs|ps|config) exec "${COMPOSE[@]}" "$operation" "$@" ;;
  shell) exec "${COMPOSE[@]}" exec so101-dev /usr/local/bin/so101-entrypoint bash ;;
  exec) container_exec "$@" ;;
  check) exec python3 "$TASK_ROOT/gui/scripts/check.py" "${COMPOSE[@]}" ;;
  recreate-check) exec python3 "$TASK_ROOT/gui/scripts/recreate_check.py" "${COMPOSE[@]}" ;;
  *) echo "Unknown operation: $operation" >&2; exit 2 ;;
esac

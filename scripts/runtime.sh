#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
TASK_BACKEND=mock
if [[ $# -gt 0 ]]; then
  case "$1" in
    mock|mujoco|real) TASK_BACKEND="$1"; shift ;;
    --*|*:=*) ;;
    *) echo 'Usage: runtime.sh [mock|mujoco|real] [--smoothing none|three_point] [launch arguments]' >&2; exit 2 ;;
  esac
fi
TASK_ARGS=()
TASK_DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --smoothing)
      if [[ $# -lt 2 ]]; then echo '--smoothing requires none or three_point' >&2; exit 2; fi
      case "$2" in none|three_point) ;; *) echo 'Unknown action smoothing method' >&2; exit 2 ;; esac
      TASK_ARGS+=("action_smoothing:=$2"); shift 2 ;;
    --smoothing=*)
      TASK_SMOOTHING="${1#*=}"
      case "$TASK_SMOOTHING" in none|three_point) ;; *) echo 'Unknown action smoothing method' >&2; exit 2 ;; esac
      TASK_ARGS+=("action_smoothing:=$TASK_SMOOTHING"); shift ;;
    --dry-run) TASK_DRY_RUN=1; shift ;;
    *) TASK_ARGS+=("$1"); shift ;;
  esac
done
if [[ "$TASK_DRY_RUN" == 1 ]]; then
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' \
    ros2 launch so101_bringup runtime.launch.py "backend:=$TASK_BACKEND" "${TASK_ARGS[@]}"
  exit 0
fi
container_start
TASK_SESSION="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
trap 'container_exec python /workspace/scripts/runtime_supervisor.py stop "$TASK_SESSION" || true' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
container_exec env \
  "FASTRTPS_DEFAULT_PROFILES_FILE=${FASTRTPS_DEFAULT_PROFILES_FILE:-/workspace/config/runtime/fastdds.xml}" \
  python /workspace/scripts/runtime_supervisor.py run "$TASK_SESSION" \
  ros2 launch so101_bringup runtime.launch.py "backend:=$TASK_BACKEND" "${TASK_ARGS[@]}"

#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
TASK_OPERATION="${1:?Usage: record.sh start TASK OPERATOR | stop true/false REASON}"
shift
if [[ "$TASK_OPERATION" == start ]]; then
  TASK_TASK="${1:?Task required}"
  TASK_OPERATOR="${2:?Operator required}"
  TASK_REQUEST="$(python3 - "$TASK_TASK" "$TASK_OPERATOR" <<'PY'
import json, sys
print(json.dumps({'task_id':sys.argv[1],'operator_name':sys.argv[2]}))
PY
)"
  container_exec ros2 service call /start_episode so101_msgs/srv/StartEpisode "$TASK_REQUEST"
elif [[ "$TASK_OPERATION" == stop ]]; then
  TASK_REQUEST="$(python3 - "${1:-false}" "${2:-}" <<'PY'
import json, sys
if sys.argv[1] not in ('true','false'): raise SystemExit('success must be true or false')
print(json.dumps({'success':sys.argv[1]=='true','failure_reason':sys.argv[2]}))
PY
)"
  container_exec ros2 service call /stop_episode so101_msgs/srv/StopEpisode "$TASK_REQUEST"
else
  exit 2
fi

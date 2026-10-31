#!/usr/bin/env bash
# Read-only GUI for the latest per-component /diagnostics status.
set -euo pipefail
exec "$(dirname "$0")/gui.sh" exec ros2 run rqt_runtime_monitor rqt_runtime_monitor "$@"

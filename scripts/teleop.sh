#!/usr/bin/env bash
# Start the runtime in IDLE; mode selection is an explicit second step.
exec "$(dirname "$0")/runtime.sh" "$@"

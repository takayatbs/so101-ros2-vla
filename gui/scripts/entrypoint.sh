#!/usr/bin/env bash
set -eo pipefail
source /etc/physical-ai/ros-shell.bash
if [[ "${GUI_SOFTWARE_RENDERING:-1}" == 1 ]]; then
  export LIBGL_ALWAYS_SOFTWARE=1
else
  unset LIBGL_ALWAYS_SOFTWARE
fi
exec "$@"

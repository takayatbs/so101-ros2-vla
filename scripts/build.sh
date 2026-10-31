#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
if [[ $SO101_NATIVE == 1 ]]; then
  mkdir -p /workspace/data
  exec flock -n -E 75 /workspace/data/.compute.lock /workspace/scripts/build_inside.sh
fi
"${COMPOSE[@]}" build
echo 'Unified image built. Save GUI work before ./scripts/gui.sh up --force-recreate (or migrate from the former standalone GUI).'
echo 'For shared ROS sources, run ./scripts/build.sh inside the GUI after startup.'

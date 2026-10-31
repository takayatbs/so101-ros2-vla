#!/usr/bin/env bash
set -euo pipefail
if [[ -f /etc/so101/unified ]]; then
  echo 'Unified GUI / SO-101 container. Host Docker checks belong on the Linux host.'
  id
  ros2 pkg prefix so101_bringup
  python -c 'import sys; print(sys.executable)'
  if command -v nvidia-smi >/dev/null; then nvidia-smi; fi
  ls -l /dev/serial/by-id /dev/v4l/by-id 2>/dev/null || true
  exit 0
fi
command -v docker >/dev/null
docker compose version
docker info --format 'Docker {{.ServerVersion}}'
if command -v nvidia-smi >/dev/null; then nvidia-smi; fi
ls -l /dev/serial/by-id /dev/v4l/by-id 2>/dev/null || true
printf '%s\n' 'Host check complete. See docs/setup.ja.md for Docker GPU/udev setup.'

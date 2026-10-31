#!/usr/bin/env bash
set -euo pipefail
umask 077
if [[ $(id -u) == 0 ]]; then
  echo 'The GUI must run as the dev user, not root.' >&2
  exit 1
fi
if [[ ! "${VNC_GEOMETRY:-1600x900}" =~ ^[0-9]{3,4}x[0-9]{3,4}$ ]]; then
  echo 'VNC_GEOMETRY must be WIDTHxHEIGHT (for example 1600x900).' >&2
  exit 1
fi
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
# Validate a runtime file, without placing the password in arguments or env.
python3 - <<'PY'
from pathlib import Path
secret = Path('/run/secrets/vnc_password').read_bytes()
password = secret.rstrip(b'\r\n')
if len(password) != 8 or any(v < 33 or v > 126 for v in password):
    raise SystemExit('VNC password must contain exactly 8 printable non-space ASCII characters.')
PY
tigervncpasswd -f < /run/secrets/vnc_password > "$XDG_RUNTIME_DIR/vnc.passwd"
chmod 600 "$XDG_RUNTIME_DIR/vnc.passwd"
touch "$XAUTHORITY"
xauth -f "$XAUTHORITY" add "$DISPLAY" . "$(mcookie)"
children=()
cleanup() {
  trap - EXIT
  for child in "${children[@]}"; do kill -TERM -- "-$child" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT
setsid Xtigervnc "$DISPLAY" -desktop 'Physical AI / ROS 2 Jazzy' \
  -geometry "${VNC_GEOMETRY:-1600x900}" -depth 24 \
  -rfbport "${VNC_RFB_PORT:-5901}" -localhost yes -SecurityTypes VncAuth \
  -PasswordFile "$XDG_RUNTIME_DIR/vnc.passwd" -AlwaysShared \
  -auth "$XAUTHORITY" -nolisten tcp &
children+=("$!")
ready=0
for attempt in {1..100}; do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then ready=1; break; fi
  kill -0 "${children[0]}" 2>/dev/null || break
  sleep 0.1
done
if [[ $ready != 1 ]]; then echo 'Xvnc did not become ready.' >&2; exit 1; fi
unset SESSION_MANAGER DBUS_SESSION_BUS_ADDRESS
setsid dbus-run-session -- mate-session &
children+=("$!")
setsid websockify --web=/usr/share/novnc/ --heartbeat=30 \
  "${GUI_BIND_ADDRESS:-127.0.0.1}:${GUI_HTTP_PORT:-6080}" "127.0.0.1:${VNC_RFB_PORT:-5901}" &
children+=("$!")
echo 'MATE started as dev. noVNC is available through the host loopback port and SSH tunnel.'
# Exit on a critical component's termination instead of leaving a black desktop.
set +e
wait -n "${children[@]}"
status=$?
set -e
exit "$status"

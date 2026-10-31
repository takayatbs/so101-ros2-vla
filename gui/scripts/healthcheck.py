#!/usr/bin/env python3
"""Readiness without a VNC login attempt (which can trigger blacklisting)."""
import os
import subprocess
import urllib.request

subprocess.run(['xdpyinfo'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
subprocess.run(['pgrep', '-x', 'mate-session'], check=True, stdout=subprocess.DEVNULL, timeout=2)
port = int(os.environ.get('GUI_HTTP_PORT', '6080'))
rfb = int(os.environ.get('VNC_RFB_PORT', '5901'))
with urllib.request.urlopen(f'http://127.0.0.1:{port}/vnc.html', timeout=2) as response:
    assert response.status == 200
# Opening and dropping an RFB handshake repeatedly is counted as failed
# authentication by TigerVNC. Observe the actual listening sockets instead.
listeners = subprocess.check_output(['ss', '-ltnH'], text=True, timeout=2)
vnc = [line.split()[3] for line in listeners.splitlines() if line.split()[3].endswith(f':{rfb}')]
assert vnc and all(address in (f'127.0.0.1:{rfb}', f'[::1]:{rfb}') for address in vnc)
http = [line.split()[3] for line in listeners.splitlines() if line.split()[3].endswith(f':{port}')]
bind = os.environ.get('GUI_BIND_ADDRESS', '127.0.0.1')
assert http and all(address in (f'{bind}:{port}', f'[::1]:{port}') for address in http)

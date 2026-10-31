#!/usr/bin/env python3
"""Host-side checks. Receives the exact Compose invocation from gui.sh."""
import datetime
import json
from pathlib import Path
import stat
import subprocess
import sys
import urllib.request

base = Path(__file__).resolve().parents[1]
compose = sys.argv[1:]

def run(*args):
    return subprocess.run([*compose, *args], check=True, capture_output=True, text=True).stdout

config = json.loads(run('config', '--format', 'json'))
service = config['services']['so101-dev']
assert service.get('network_mode') != 'host'
assert all(p['host_ip'] == '127.0.0.1' and p['target'] in (6080, 8765) for p in service['ports'])
assert not service.get('privileged', False)
assert not any(v.get('target') == '/var/run/docker.sock' for v in service['volumes'])
assert any(v.get('target') == '/dev' for v in service['volumes'])
secret = base/'.secrets/vnc_password'
assert stat.S_IMODE(secret.stat().st_mode) == 0o600
assert stat.S_IMODE(secret.parent.stat().st_mode) == 0o700
container_id = run('ps', '-q', 'so101-dev').strip()
assert container_id, 'Start the desktop first: ./scripts/gui.sh up'
inspection = json.loads(subprocess.check_output(['docker', 'inspect', container_id], text=True))[0]
bindings = inspection['HostConfig']['PortBindings']
assert set(bindings) == {'6080/tcp', '8765/tcp'}
assert all(p['HostIp'] == '127.0.0.1' for entries in bindings.values() for p in entries)
assert inspection['Config']['User'] not in ('', '0', 'root')
for mount in inspection['Mounts']:
    if mount['Destination'] == '/run/secrets/vnc_password':
        assert not mount['RW']
port = next(p['published'] for p in service['ports'] if p['target'] == 6080)
with urllib.request.urlopen(f'http://127.0.0.1:{port}/vnc.html', timeout=10) as response:
    assert response.status == 200
result = json.loads(run('exec', '-T', 'so101-dev', '/usr/local/bin/so101-entrypoint',
                        'flock', '-n', '-E', '75', '/workspace/data/.compute.lock', '/usr/local/bin/physical-ai-verify'))
result['security'] = {'host_loopback_only': True, 'direct_vnc_not_published': True,
                      'secret_permissions': '0600', 'non_root': True, 'docker_socket_not_shared': True}
result['container_id'] = container_id
result['image_id'] = inspection['Image']
result['gpu_requested'] = bool(inspection['HostConfig'].get('DeviceRequests'))
result['vnc_listeners'] = run('exec', '-T', 'so101-dev', 'ss', '-ltn')
# Check actual sockets too: X11 TCP must not be listening and VNC is loopback.
rfb = str(service['environment'].get('VNC_RFB_PORT', '5901'))
vnc_lines = [line for line in result['vnc_listeners'].splitlines() if ':'+rfb+' ' in line]
assert vnc_lines and all('127.0.0.1:'+rfb in line or '[::1]:'+rfb in line for line in vnc_lines)
assert ':6001 ' not in result['vnc_listeners']
gpu = subprocess.run([*compose, 'exec', '-T', 'so101-dev', 'nvidia-smi', '--query-gpu=name,driver_version', '--format=csv,noheader'], capture_output=True, text=True)
result['nvidia_smi'] = {'returncode': gpu.returncode, 'output': gpu.stdout.strip(), 'error': gpu.stderr.strip()}
result['checked_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
artifacts = base/'artifacts'
artifacts.mkdir(exist_ok=True)
path = artifacts/('check-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False, indent=2))
print('Saved:', path)

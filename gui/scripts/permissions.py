#!/usr/bin/env python3
"""Repair root-owned generated files using the configured desktop UID/GID."""
import datetime
import fcntl
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
compose = sys.argv[1:]
config = json.loads(subprocess.check_output([*compose, 'config', '--format', 'json'], text=True))
service = config['services']['so101-dev']
uid, gid = service['user'].split(':')
(root/'data').mkdir(exist_ok=True)
with (root/'data/.compute.lock').open('a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Stop Runtime/training/calibration/test before fixing generated-file ownership.')
    command = ['docker', 'run', '--rm', '--user', '0', '--entrypoint', 'python3',
               '--mount', f'type=bind,src={root},dst=/workspace', service['image'],
               '/workspace/scripts/fix_permissions.py', uid, gid]
    before = subprocess.check_output(command, text=True)
    artifacts = root/'gui/artifacts'
    artifacts.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    (artifacts/('permissions-before-'+stamp+'.json')).write_text(before)
    subprocess.run([*command, '--apply'], check=True, stdout=subprocess.DEVNULL)
print('Generated-file ownership repaired. Non-root files and file modes were preserved.')

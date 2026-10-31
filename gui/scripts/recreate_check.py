#!/usr/bin/env python3
"""Explicitly recreates only the GUI service, then verifies bind and home data."""
import datetime
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import uuid

base = Path(__file__).resolve().parents[1]
compose = sys.argv[1:]
compute_lock = (base.parent/'data/.compute.lock').open('a')
try:
    fcntl.flock(compute_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit('Stop Runtime/training/calibration/test before recreating the unified container.')

def run(*args):
    return subprocess.run([*compose, *args], check=True, capture_output=True, text=True).stdout.strip()

before = run('ps', '-q', 'so101-dev')
assert before, 'Start the GUI before the recreation test.'
marker = 'gui-persistence-'+uuid.uuid4().hex
source = base.parent/'data'/(marker+'.txt')
home = '/home/dev/'+marker+'.txt'
source.write_text(marker+'\n')
# Contents stay in an argument here because this is a test marker, never a secret.
run('exec', '-T', 'so101-dev', 'python3', '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])', home, marker)
try:
    run('up', '-d', '--force-recreate', '--wait', '--wait-timeout', '120', 'so101-dev')
    after = run('ps', '-q', 'so101-dev')
    assert after and before != after, 'Container was not recreated.'
    assert source.read_text().strip() == marker
    assert run('exec', '-T', 'so101-dev', 'cat', '/workspace/data/'+source.name) == marker
    assert run('exec', '-T', 'so101-dev', 'cat', home) == marker
    result = {'result': 'PASS', 'bind_source_persisted': True, 'home_volume_persisted': True,
              'container_recreated': True, 'before': before, 'after': after}
    artifacts = base/'artifacts'
    artifacts.mkdir(exist_ok=True)
    target = artifacts/('persistence-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    target.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
finally:
    source.unlink(missing_ok=True)
    run('exec', '-T', 'so101-dev', 'rm', '-f', home)

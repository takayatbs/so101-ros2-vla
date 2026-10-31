#!/usr/bin/env python3
"""Create local settings and a secret, preserving existing values."""
import argparse
import getpass
import grp
import os
from pathlib import Path
import secrets
import string
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--generate-password', action='store_true')
parser.add_argument('--reset-password', action='store_true', help='Replace the VNC password after validating the new input')
args = parser.parse_args()
base = Path(__file__).resolve().parents[1]
root = base.parent
(root/'data').mkdir(exist_ok=True)
env = root/'.env'
# Preserve existing host settings when adding new desktop defaults.
def settings(path):
    if not path.exists():
        return {}
    return dict(line.split('=', 1) for line in path.read_text().splitlines()
                if line and not line.startswith('#') and '=' in line)

current = settings(env)
defaults = settings(root/'.env.example')
defaults['DEV_UID'] = str(os.getuid())
defaults['DEV_GID'] = str(os.getgid())
for key, group in [('SO101_DIALOUT_GID', 'dialout'), ('SO101_VIDEO_GID', 'video')]:
    try:
        defaults[key] = str(grp.getgrnam(group).gr_gid)
    except KeyError:
        pass
if not env.exists():
    env.write_text((root/'.env.example').read_text())
    current = settings(env)
    # Replace example IDs on a new installation only.
    text = env.read_text()
    for key in ['DEV_UID', 'DEV_GID', 'SO101_DIALOUT_GID', 'SO101_VIDEO_GID']:
        text = '\n'.join(key+'='+defaults[key] if line.startswith(key+'=') else line
                         for line in text.split('\n'))
    env.write_text(text)
    env.chmod(0o600)
else:
    additions = {k: v for k, v in defaults.items() if k not in current}
    if additions:
        with env.open('a') as output:
            output.write('\n# Unified GUI / SO-101 settings\n')
            output.writelines(k+'='+v+'\n' for k, v in additions.items())
folder = base/'.secrets'
folder.mkdir(mode=0o700, exist_ok=True)
folder.chmod(0o700)
secret = folder/'vnc_password'
if args.reset_password or not secret.exists():
    if args.generate_password:
        password = ''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(8))
    else:
        password = getpass.getpass('VNC password (exactly 8 non-space ASCII characters): ')
        if password != getpass.getpass('Confirm VNC password: '):
            raise SystemExit('Passwords did not match. The password file was not changed.')
    if len(password) != 8 or any(not 33 <= ord(v) <= 126 for v in password):
        raise SystemExit('Use exactly 8 printable non-space ASCII characters.')
    if args.reset_password:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=folder, prefix='.vnc-password-', delete=False) as output:
                temporary = Path(output.name)
                output.write(password+'\n')
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, secret)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    else:
        with secret.open('x') as output:
            output.write(password+'\n')
        secret.chmod(0o600)
if args.reset_password:
    print('VNC password updated. Existing environment settings were preserved.')
    print('Save open GUI work, then recreate the container to apply the new password.')
else:
    print('Unified settings (.env) and VNC password file are ready. Existing values were preserved.')
print('Password file: gui/.secrets/vnc_password (never commit or paste into a URL).')

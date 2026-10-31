#!/usr/bin/env python3
"""Give only root-owned generated project files to the desktop user."""
import argparse
import json
import os
from pathlib import Path
import stat

parser = argparse.ArgumentParser()
parser.add_argument('uid', type=int)
parser.add_argument('gid', type=int)
parser.add_argument('--apply', action='store_true')
args = parser.parse_args()
assert args.uid > 0 and args.gid > 0
root = Path('/workspace')
changed = []
for relative in ['data', 'config', 'ros2_ws/build', 'ros2_ws/install', 'ros2_ws/log']:
    folder = root/relative
    if not folder.exists() or folder.is_symlink():
        continue
    paths = [folder]
    for base, directories, files in os.walk(folder, followlinks=False):
        paths.extend(Path(base)/name for name in directories+files)
    for path in paths:
        metadata = path.lstat()
        if metadata.st_uid != 0:
            continue
        changed.append({'path': str(path.relative_to(root)), 'uid': metadata.st_uid,
                        'gid': metadata.st_gid, 'mode': stat.S_IMODE(metadata.st_mode)})
        if args.apply:
            os.chown(path, args.uid, args.gid, follow_symlinks=False)
print(json.dumps({'applied': args.apply, 'files': changed}, indent=2))

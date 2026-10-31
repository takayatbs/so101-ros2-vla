#!/usr/bin/env python3
"""Audit files eligible for publication, without staging or changing the repository."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import unquote


PATTERNS = {
    'personal home path': re.compile(r'/(?:home/(?!dev(?:/|\b))|Users/)[\w.\-]+/'),
    'private key': re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----'),
    'access token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|hf_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b'),
    'credential in URL': re.compile(r'https?://[^\s/:@]+:[^\s/@]+@'),
    'concrete serial device': re.compile(r'/dev/(?:serial|v4l)/by-id/(?![^\s`"\']*YOUR_)[A-Za-z0-9_]+-[A-Za-z0-9_]+-[A-Za-z0-9_.-]+'),
}
IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
DOCUMENTATION_NETS = tuple(ipaddress.ip_network(n) for n in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24'))
PRIVATE_NAMES = {'.env', '.envrc', '.netrc', '.git-credentials', '.pypirc', '.npmrc', 'vnc_password'}


def publication_files(root):
    """Use repository ignore rules even before git init; include tracked mistakes."""
    with tempfile.TemporaryDirectory(prefix='so101-public-index-') as directory:
        subprocess.run(['git', 'init', '--bare', '--quiet', directory], check=True, capture_output=True)
        command = ['git', '-c', 'core.excludesFile=/dev/null', '--git-dir='+directory, '--work-tree='+str(root)]
        result = subprocess.check_output([*command, 'ls-files', '--others', '--exclude-standard', '-z'])
        files = set(result.decode().split('\0'))-{''}
    top = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'], text=True, capture_output=True)
    tracked_ignored = []
    if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root:
        tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '--cached', '-z'])
        files.update(set(tracked.decode().split('\0'))-{''})
        ignored = subprocess.run(['git', '-C', str(root), 'check-ignore', '--no-index', '--stdin', '-z'],
                                 input=tracked, capture_output=True, check=False)
        if ignored.returncode not in (0, 1):
            raise RuntimeError('git check-ignore failed')
        tracked_ignored = sorted(set(ignored.stdout.decode().split('\0'))-{''})
    return sorted(files), tracked_ignored


def audit(root):
    files, tracked_ignored = publication_files(root)
    available = set(files)
    issues = [{'path': name, 'reason': 'already tracked despite ignore rule'} for name in tracked_ignored]
    total = 0
    for name in files:
        path = root/name
        if not path.exists() or path.is_symlink() or not path.is_file():
            issues.append({'path': name, 'reason': 'missing, non-file or symbolic link'})
            continue
        size = path.stat().st_size
        total += size
        if size > 5*1024*1024:
            issues.append({'path': name, 'reason': 'file exceeds 5 MiB; review generated data'})
            continue
        if path.name in PRIVATE_NAMES or (path.name.startswith('.env.') and path.name != '.env.example'):
            issues.append({'path': name, 'reason': 'private settings or credentials'})
        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            issues.append({'path': name, 'reason': 'binary file requires explicit review'})
            continue
        for number, line in enumerate(content.splitlines(), 1):
            for reason, pattern in PATTERNS.items():
                if pattern.search(line):
                    issues.append({'path': name, 'line': number, 'reason': reason})
            for match in IPV4.finditer(line):
                try:
                    address = ipaddress.ip_address(match.group())
                except ValueError:
                    continue
                if not address.is_loopback and not address.is_unspecified and not any(address in net for net in DOCUMENTATION_NETS):
                    issues.append({'path': name, 'line': number, 'reason': 'concrete IP address'})
            if name == 'config/hardware/robot.yaml' and re.match(r'^hardware_qualified:\s*true\b', line):
                issues.append({'path': name, 'line': number, 'reason': 'public hardware must start unqualified'})
        if path.suffix == '.md':
            for target in re.findall(r'\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)', content):
                target = unquote(target.strip('<>')).split('#', 1)[0]
                if not target or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target):
                    continue
                resolved = (path.parent/target).resolve()
                try:
                    relative = resolved.relative_to(root).as_posix()
                except ValueError:
                    issues.append({'path': name, 'reason': 'documentation link leaves repository'})
                    continue
                if relative not in available and not any(p.startswith(relative.rstrip('/')+'/') for p in available):
                    issues.append({'path': name, 'reason': 'documentation link target is not published', 'target': target})
    return {'ok': not issues, 'candidate_count': len(files), 'total_bytes': total,
            'issues': issues, 'files': files,
            'scope': 'Current files and common leak patterns; not a complete secret detector or Git history audit.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--json', type=Path, help='Save the audit and eligible file list')
    parser.add_argument('--export', type=Path, help='Copy audited files to a new review directory; never publish')
    args = parser.parse_args()
    root = args.root.resolve()
    result = audit(root)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'files'}, ensure_ascii=False, indent=2))
    if not result['ok']:
        return 1
    if args.export:
        if args.export.exists():
            parser.error('--export must name a directory that does not exist')
        args.export.mkdir(parents=True)
        for name in result['files']:
            destination = args.export/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root/name, destination)
        print('Exported audited files to '+str(args.export))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

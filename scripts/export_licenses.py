#!/usr/bin/env python3
"""Export installed package license evidence; does not fetch or execute packages."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
LICENSE_PREFIXES = ('license', 'licence', 'copying', 'notice', 'copyright', 'authors', 'eula')


def is_license_file(path):
    path = Path(path)
    return (path.suffix.lower() not in {'.py', '.pyc', '.so', '.dll', '.exe'}
            and '__pycache__' not in path.parts
            and (path.name.lower().startswith(LICENSE_PREFIXES)
                 or any(p.lower() in {'licenses', 'licences'} for p in path.parts)))


def python_snapshot():
    """Read distribution metadata without importing any dependency's code."""
    result = []
    for dist in importlib.metadata.distributions():
        metadata = dist.metadata
        name = metadata.get('Name')
        if not name:
            continue
        texts = []
        for relative in dist.files or []:
            source = Path(dist.locate_file(relative))
            if is_license_file(relative) and source.is_file():
                texts.append({'name': str(relative), 'source': str(source)})
        # PathDistribution exposes the metadata directory here. Some Debian
        # egg-info packages have no RECORD/SOURCES list, so dist.files is empty.
        directory = getattr(dist, '_path', None)
        metadata_file = None
        if directory is not None:
            if Path(directory).is_file():
                metadata_file = str(directory)
            for filename in ('METADATA', 'PKG-INFO'):
                candidate = Path(directory)/filename
                if candidate.is_file():
                    metadata_file = str(candidate)
                    break
        result.append({'name': name, 'version': dist.version,
                       'license_expression': metadata.get('License-Expression'),
                       'license_metadata': metadata.get('License'),
                       'license_classifiers': [c for c in metadata.get_all('Classifier', [])
                                               if c.startswith('License ::')],
                       'project_urls': metadata.get_all('Project-URL', []),
                       'files': texts, '_metadata_file': metadata_file})
    return sorted(result, key=lambda item: (item['name'].lower(), item['version']))


def safe_name(value):
    return re.sub(r'[^a-zA-Z0-9_.+-]', '_', value)


def copy_evidence(source, destination, relative_to):
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Resolve symlinks to preserve a readable copy of Debian common licenses.
    shutil.copyfile(source, destination)
    return destination.relative_to(relative_to).as_posix()


def supplemental(record, ecosystem):
    manifest = ROOT/'LICENSES/runtime-manifest.json'
    if not manifest.is_file():
        return []
    normalize = lambda s: re.sub(r'[-_.]+', '-', s).lower()
    entries = json.loads(manifest.read_text())['packages']
    for entry in entries:
        if (entry['ecosystem'] != ecosystem or entry['version'] != record['version']
                or normalize(entry['name']) != normalize(record['name'])):
            continue
        for text in entry['texts']:
            source = (ROOT/text['path']).resolve()
            if not source.is_relative_to(ROOT/'LICENSES'):
                raise ValueError('Supplementary license path leaves LICENSES')
            if hashlib.sha256(source.read_bytes()).hexdigest() != text['sha256']:
                raise ValueError('Supplementary license checksum mismatch: '+text['path'])
        return entry['texts']
    return []


def collect_python(interpreter, destination, bundle, debian):
    raw = subprocess.check_output([str(interpreter), str(Path(__file__).resolve()),
                                   '--python-snapshot'], text=True)
    records = json.loads(raw)
    ownership = {}
    anchors = sorted({r['_metadata_file'] for r in records if r['_metadata_file']})
    if anchors and shutil.which('dpkg-query'):
        query = subprocess.run(['dpkg-query', '-S', '--', *anchors],
                               text=True, capture_output=True, check=False)
        if query.returncode not in (0, 1):
            raise RuntimeError('Unable to query Debian ownership')
        for line in query.stdout.splitlines():
            owner, separator, filename = line.rpartition(': ')
            if separator:
                ownership[filename] = owner
    debian_packages = {p['name']: p for p in debian['packages']}
    for number, record in enumerate(records):
        folder = destination/f'{number:04d}-{safe_name(record["name"])}-{safe_name(record["version"])}'
        paths = []
        for index, entry in enumerate(record.pop('files')):
            paths.append(copy_evidence(entry['source'], folder/'texts'/
                                      f'{index:03d}-{safe_name(Path(entry["name"]).name)}', bundle))
        embedded = record.get('license_metadata') or ''
        if len(embedded) > 200:
            path = folder/'LICENSE.metadata.txt'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(embedded+'\n')
            paths.append(path.relative_to(bundle).as_posix())
        metadata_file = record.pop('_metadata_file')
        owner = ownership.get(metadata_file)
        if owner in debian_packages:
            record['debian_package'] = owner
            paths.extend(debian_packages[owner]['license_files'])
        if not paths:
            texts = supplemental(record, 'python')
            record['supplementary_sources'] = texts
            paths.extend('project/'+text['path'] for text in texts)
        if not paths and metadata_file and Path(metadata_file).is_relative_to(ROOT/'ros2_ws'):
            # Locally built project packages are covered by the project bundle.
            name = record['name'].replace('-', '_')
            if (ROOT/'ros2_ws/src'/name/'package.xml').is_file():
                paths.append('project/LICENSE')
                if name == 'so101_core':
                    paths.append('project/NOTICE')
        record['license_files'] = paths
        record['needs_review'] = not bool(paths)
    return {'interpreter': str(interpreter), 'packages': records}


def collect_debian(bundle):
    if not shutil.which('dpkg-query'):
        return {'available': False, 'packages': []}
    fmt = '${db:Status-Status}\t${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n'
    raw = subprocess.check_output(['dpkg-query', '-W', '-f='+fmt], text=True)
    records = []
    for line in raw.splitlines():
        status, name, version, source, source_version = line.split('\t')
        if status != 'installed':
            continue
        copyright_file = Path('/usr/share/doc')/name.split(':')[0]/'copyright'
        paths = []
        if copyright_file.is_file():
            paths.append(copy_evidence(copyright_file, bundle/'debian'/safe_name(name)/'copyright', bundle))
        record = {'name': name, 'version': version, 'source_package': source,
                  'source_version': source_version}
        if not paths:
            texts = supplemental(record, 'debian')
            record['supplementary_sources'] = texts
            paths.extend('project/'+text['path'] for text in texts)
        record.update(license_files=paths, needs_review=not bool(paths))
        records.append(record)
    for path in Path('/usr/share/common-licenses').glob('*'):
        if path.is_file():
            copy_evidence(path, bundle/'debian/common-licenses'/path.name, bundle)
    return {'available': True, 'packages': records}


def export(output, interpreters):
    output = Path(output).absolute()
    if output.exists():
        raise FileExistsError('Output already exists; choose a new directory: '+str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.licenses.partial-', dir=output.parent) as temporary:
        bundle = Path(temporary)/'bundle'
        bundle.mkdir()
        for name in ['LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md']:
            if (ROOT/name).is_file():
                copy_evidence(ROOT/name, bundle/'project'/name, bundle)
        for path in (ROOT/'LICENSES').rglob('*'):
            if path.is_file():
                copy_evidence(path, bundle/'project'/path.relative_to(ROOT), bundle)
        debian = collect_debian(bundle)
        python = [collect_python(executable, bundle/'python'/str(index), bundle, debian)
                  for index, executable in enumerate(interpreters)]
        manifest = {'generated_at': datetime.now(timezone.utc).isoformat(),
                    'scope': 'Installed metadata and shipped notices, not a source-code offer or a complete legal review.',
                    'python': python, 'debian': debian}
        (bundle/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
        all_records = [p for env in python for p in env['packages']]+debian['packages']
        missing = [p for p in all_records if p['needs_review']]
        readme = ('# Installed dependency license evidence\n\n'
                  'See manifest.json for versions, source-package versions, license metadata and copied texts.\n'
                  'Metadata alone is not proof of license compliance. Review file-specific licenses,\n'
                  'bundled native libraries, model weights, and corresponding-source obligations before distribution.\n'
                  'This bundle does not contain corresponding source code.\n\n'
                  f'Package records: {len(all_records)}. Records without a license-text file: {len(missing)}.\n'
                  'Such records are marked needs_review; a license identifier is not a substitute for the text.\n')
        (bundle/'README.txt').write_text(readme)
        bundle.rename(output)
    return {'output': str(output), 'package_records': len(all_records),
            'records_without_license_text': len(missing)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New directory, normally under data/licenses/')
    parser.add_argument('--python', type=Path, action='append', dest='interpreters',
                        help='Python environment to inspect; repeat to include multiple environments')
    parser.add_argument('--python-snapshot', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.python_snapshot:
        print(json.dumps(python_snapshot()))
        return
    if not args.output:
        parser.error('--output is required')
    interpreters = args.interpreters or [Path(sys.executable)]
    # Inspect the optional policy environment as well by default.
    policies = ROOT/'data/envs/policies/bin/python'
    if not args.interpreters and policies.exists() and str(policies) != sys.executable:
        interpreters.append(policies)
    print(json.dumps(export(args.output, interpreters), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

"""Distribution evidence must retain actual notices and expose missing texts."""
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def exporter():
    spec = importlib.util.spec_from_file_location('export_licenses', ROOT/'scripts/export_licenses.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_does_not_import_package_and_retains_vendored_notices(tmp_path, monkeypatch, exporter):
    package = tmp_path/'example'
    package.mkdir()
    (package/'__init__.py').write_text('raise RuntimeError("Do not import this package")\n')
    metadata = tmp_path/'example-1.2.dist-info'
    (metadata/'licenses/vendor').mkdir(parents=True)
    (metadata/'METADATA').write_text('Name: example\nVersion: 1.2\nLicense-Expression: MIT\n')
    (metadata/'licenses/LICENSE').write_text('Example copyright and permission notice\n')
    (metadata/'licenses/vendor/NOTICE.txt').write_text('Vendor attribution\n')
    (metadata/'RECORD').write_text('\n'.join(n+',,' for n in [
        'example/__init__.py', 'example-1.2.dist-info/METADATA',
        'example-1.2.dist-info/licenses/LICENSE', 'example-1.2.dist-info/licenses/vendor/NOTICE.txt']))
    distribution = importlib.metadata.Distribution.at(metadata)
    monkeypatch.setattr(exporter.importlib.metadata, 'distributions', lambda: [distribution])
    record, = exporter.python_snapshot()
    assert record['name'] == 'example'
    assert record['license_expression'] == 'MIT'
    assert {Path(x['source']).read_text() for x in record['files']} == {
        'Example copyright and permission notice\n', 'Vendor attribution\n'}
    assert not exporter.is_license_file('licenses/__pycache__/AUTHORS.cpython-312.pyc')


def test_copies_full_notice_and_marks_missing_license_instead_of_inventing_it(tmp_path, monkeypatch, exporter):
    upstream = tmp_path/'upstream-NOTICE'
    upstream.write_text('Copyright owner\nRetain this notice verbatim.\n')
    records = [
        {'name': 'with-notice', 'version': '1', 'license_metadata': 'MIT', '_metadata_file': None,
         'files': [{'name': 'dist-info/NOTICE', 'source': str(upstream)}]},
        {'name': 'identifier-only', 'version': '1', 'license_metadata': 'BSD', '_metadata_file': None,
         'files': []},
    ]
    monkeypatch.setattr(exporter.subprocess, 'check_output', lambda *a, **kw: json.dumps(records))
    bundle = tmp_path/'bundle'
    result = exporter.collect_python(Path(sys.executable), bundle/'python', bundle, {'packages': []})
    present, missing = result['packages']
    assert not present['needs_review']
    assert (bundle/present['license_files'][0]).read_bytes() == upstream.read_bytes()
    assert missing['needs_review'] and not missing['license_files']
    assert all('_metadata_file' not in r for r in result['packages'])


def test_debian_ownership_connects_python_metadata_to_shipped_copyright(tmp_path, monkeypatch, exporter):
    records = [{'name': 'example', 'version': '1', 'license_metadata': 'MIT', 'files': [],
                '_metadata_file': '/usr/lib/example.dist-info/METADATA'}]
    monkeypatch.setattr(exporter.subprocess, 'check_output', lambda *a, **kw: json.dumps(records))
    monkeypatch.setattr(exporter.shutil, 'which', lambda name: '/usr/bin/dpkg-query')
    def query(args, **kwargs):
        assert args[:3] == ['dpkg-query', '-S', '--']
        return subprocess.CompletedProcess(args, 0,
            stdout='python3-example: /usr/lib/example.dist-info/METADATA\n', stderr='')
    monkeypatch.setattr(exporter.subprocess, 'run', query)
    debian = {'packages': [{'name': 'python3-example', 'license_files': ['debian/python3-example/copyright']}]}
    record, = exporter.collect_python(Path(sys.executable), tmp_path/'python', tmp_path, debian)['packages']
    assert record['debian_package'] == 'python3-example'
    assert record['license_files'] == ['debian/python3-example/copyright']
    assert not record['needs_review']


def test_failed_export_is_atomic_and_existing_bundle_is_preserved(tmp_path, monkeypatch, exporter):
    monkeypatch.setattr(exporter, 'collect_debian', lambda bundle: {'packages': []})
    def fail(*args):
        raise RuntimeError('Environment unavailable')
    monkeypatch.setattr(exporter, 'collect_python', fail)
    output = tmp_path/'bundle'
    with pytest.raises(RuntimeError, match='Environment unavailable'):
        exporter.export(output, [Path('/missing/python')])
    assert not output.exists()
    assert not list(tmp_path.glob('.licenses.partial-*'))
    output.mkdir()
    marker = output/'NOTICE'
    marker.write_text('Keep the previous release evidence\n')
    with pytest.raises(FileExistsError):
        exporter.export(output, [])
    assert marker.read_text() == 'Keep the previous release evidence\n'


def test_supplementary_text_is_version_specific_and_rejects_tampering(tmp_path, monkeypatch, exporter):
    import hashlib
    licenses = tmp_path/'LICENSES'
    licenses.mkdir()
    text = licenses/'example.txt'
    text.write_text('Copyright holder and full permission notice\n')
    digest = hashlib.sha256(text.read_bytes()).hexdigest()
    (licenses/'runtime-manifest.json').write_text(json.dumps({'packages': [{
        'ecosystem': 'python', 'name': 'example-package', 'version': '1.2',
        'texts': [{'path': 'LICENSES/example.txt', 'sha256': digest,
                   'source_url': 'https://example.invalid/upstream/LICENSE'}]}]}))
    monkeypatch.setattr(exporter, 'ROOT', tmp_path)
    assert exporter.supplemental({'name': 'Example_Package', 'version': '1.3'}, 'python') == []
    assert exporter.supplemental({'name': 'Example_Package', 'version': '1.2'}, 'python')[0]['sha256'] == digest
    text.write_text('Edited license terms\n')
    with pytest.raises(ValueError, match='checksum mismatch'):
        exporter.supplemental({'name': 'Example_Package', 'version': '1.2'}, 'python')


def test_each_ros_package_carries_the_declared_license_text():
    packages = list((ROOT/'ros2_ws/src').glob('*/package.xml'))
    assert packages
    for package in packages:
        assert '<license>Apache-2.0</license>' in package.read_text()
        assert (package.parent/'LICENSE').read_bytes() == (ROOT/'LICENSE').read_bytes()


def test_core_package_install_includes_license_and_third_party_notices(tmp_path):
    package = tmp_path/'core'
    shutil.copytree(ROOT/'ros2_ws/src/so101_core', package,
                    ignore=shutil.ignore_patterns('__pycache__', '*.egg-info'))
    destination = tmp_path/'installed'
    subprocess.run([sys.executable, 'setup.py', 'install_data', '--install-dir', str(destination)],
                   cwd=package, check=True, capture_output=True, text=True)
    installed = destination/'share/so101_core'
    assert (installed/'LICENSE').read_bytes() == (ROOT/'LICENSE').read_bytes()
    assert (installed/'NOTICE').read_bytes() == (ROOT/'NOTICE').read_bytes()
    for license_file in (ROOT/'LICENSES').glob('*.txt'):
        assert (installed/'LICENSES'/license_file.name).read_bytes() == license_file.read_bytes()

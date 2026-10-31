"""The optional model installer must work without a neighboring source checkout."""
import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def installer():
    spec = importlib.util.spec_from_file_location('policy_setup', ROOT/'scripts/install_policy_env.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def upstream(tmp_path):
    source = tmp_path/'test-upstream'
    source.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(source)], check=True)
    (source/'pyproject.toml').write_text('[project]\nname = "lerobot"\nversion = "0.0.0"\n')
    subprocess.run(['git', '-C', str(source), 'add', 'pyproject.toml'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '--quiet', '-m', 'Synthetic source'], check=True)
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    return source, revision


def test_source_is_pinned_in_project_and_reused_without_upstream(tmp_path, upstream):
    source, revision = upstream
    project = tmp_path/'standalone project with spaces'
    module = installer()
    target = module.prepare_source(project, str(source), revision)
    assert target.is_relative_to(project/'data/envs')
    assert subprocess.check_output(['git', '-C', str(target), 'rev-parse', 'HEAD'], text=True).strip() == revision
    shutil.rmtree(source)
    assert module.prepare_source(project, str(source), revision) == target
    assert not list((project/'data/envs').glob('*.partial-*'))


def test_modified_cache_is_preserved_and_rejected(tmp_path, upstream):
    source, revision = upstream
    module = installer()
    project = tmp_path/'project'
    target = module.prepare_source(project, str(source), revision)
    path = target/'pyproject.toml'
    changed = path.read_text()+'# local change\n'
    path.write_text(changed)
    with pytest.raises(ValueError, match='modified'):
        module.prepare_source(project, str(source), revision)
    assert path.read_text() == changed


def test_failed_fetch_leaves_no_partial_source_or_false_cache(tmp_path):
    project = tmp_path/'project'
    with pytest.raises(subprocess.CalledProcessError):
        installer().prepare_source(project, str(tmp_path/'missing-upstream'), 'a'*40)
    assert list((project/'data/envs').iterdir()) == []


def test_revision_requires_immutable_commit_before_creating_files(tmp_path):
    project = tmp_path/'project'
    with pytest.raises(ValueError, match='full commit'):
        installer().prepare_source(project, 'unused', 'main')
    assert not project.exists()

"""Public defaults must work without local hardware, data or credentials."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from so101_core.config import config_hash, load_config, load_system, resolve

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_profile_replaces_whole_file_preserves_resources_and_fingerprint(tmp_path, monkeypatch):
    monkeypatch.delenv('SO101_USE_LOCAL_CONFIG', raising=False)
    root = tmp_path/'config'
    (root/'runtime').mkdir(parents=True)
    (root/'hardware').mkdir()
    path = root/'hardware/robot.yaml'
    value = {'leader': {'calibration': '../calibration/leader.json'}, 'hardware_qualified': True}
    path.write_text(yaml.safe_dump(value))
    previous = load_config(path)
    local = path.with_name('robot.local.yaml')
    local.write_text(path.read_text())
    path.write_text('hardware_qualified: false\npublic_only: example\n')
    selected = load_config(path)
    assert selected['_file'] == str(local)
    assert selected['_config_root'] == str(root)
    assert 'public_only' not in selected
    assert config_hash(selected) == config_hash(previous)
    assert resolve(selected, selected['leader']['calibration']).resolve() == root/'calibration/leader.json'
    monkeypatch.setenv('SO101_USE_LOCAL_CONFIG', '0')
    assert load_config(path)['hardware_qualified'] is False
    assert load_config(local)['hardware_qualified'] is True


def test_invalid_local_file_is_rejected_without_silent_public_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv('SO101_USE_LOCAL_CONFIG', raising=False)
    path = tmp_path/'robot.yaml'
    path.write_text('hardware_qualified: false\n')
    path.with_name('robot.local.yaml').write_text('broken: [\n')
    with pytest.raises(yaml.YAMLError):
        load_config(path)


def test_local_manifest_and_children_resolve_from_legacy_entrypoint(tmp_path, monkeypatch):
    monkeypatch.delenv('SO101_USE_LOCAL_CONFIG', raising=False)
    root = tmp_path/'config'
    (root/'runtime').mkdir(parents=True)
    (root/'policy').mkdir()
    (root/'runtime/system.yaml').write_text('files: {policy: ../policy/policy.yaml}\n')
    (root/'runtime/system.local.yaml').write_text('files: {policy: ../policy/policy.alt.yaml}\n')
    (root/'policy/policy.yaml').write_text('model_id: public-example\n')
    (root/'policy/policy.alt.yaml').write_text('model_id: alternate-example\n')
    (root/'policy/policy.alt.local.yaml').write_text('model_id: local-model\n')
    assert load_system(root/'system.yaml')['policy']['model_id'] == 'local-model'
    selected = load_system(root/'runtime/system.yaml')['policy']
    assert selected['_config_root'] == str(root)
    monkeypatch.setenv('SO101_USE_LOCAL_CONFIG', '0')
    assert load_system(root/'runtime/system.yaml')['policy']['model_id'] == 'public-example'


def test_private_initializer_preserves_existing_files_and_public_templates(tmp_path):
    root = tmp_path/'config'
    for folder, name in [('hardware', 'robot'), ('control', 'teleop'), ('collection', 'record'), ('cameras', 'camera')]:
        (root/folder).mkdir(parents=True)
        (root/folder/f'{name}.yaml').write_text('example: true\n')
    local = root/'hardware/robot.local.yaml'
    local.write_text('private: retained\n')
    module = script('init_local_config')
    assert len(module.initialize(root)) == 3
    assert module.initialize(root) == []
    assert local.read_text() == 'private: retained\n'
    assert all(path.read_text() == 'example: true\n' for path in root.glob('*/*.yaml') if '.local.' not in path.name)


def test_ignore_rules_exclude_private_outputs_and_keep_source_and_examples(tmp_path):
    shutil.copy2(ROOT/'.gitignore', tmp_path/'.gitignore')
    keep = ['.env.example', 'data/README.md', '.devcontainer/devcontainer.json',
            'config/hardware/robot.yaml', 'config/calibration/camera.example.yaml',
            'scripts/analysis/compare_camera_latency.py', 'tests/fixtures/synthetic.json']
    private = ['.env', '.env.production', 'gui/.env', 'gui/.secrets/vnc_password',
               'config/hardware/robot.local.yaml', 'config/runtime/system.local.yaml',
               'config/calibration/leader.json', 'config/calibration/camera.yaml',
               'config/collection/record.eraser.yaml', 'data/logs/events.jsonl',
               'data/envs/python/bin/python', 'data/models/checkpoint/config.json',
               'eda/figures/frame.png', 'gui/artifacts/desktop.png', '.ruff_cache/item',
               'ros2_ws/build/result', 'ros2_ws/install/setup.bash', 'gui_ws/log/log.txt',
               'nested/.venv/bin/python', 'nested/.secrets/token', 'download/model.safetensors',
               'download/model.pt', 'capture/raw.mcap', 'capture/video.mp4', 'dataset/frames.parquet',
               'secrets/client.pem', 'model.pkl', 'desktop.log', '.DS_Store',
               '.cache/huggingface/token', 'config/hardware/robot.local.yaml.partial-test',
               'nested/.env.production', 'download/model.bin', 'backup.tar.gz', 'credentials.json']
    for name in keep+private:
        path = tmp_path/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('example\n')
    candidates, tracked = script('audit_public').publication_files(tmp_path)
    assert set(keep).issubset(candidates)
    assert not set(private).intersection(candidates)
    assert not tracked


def test_audit_detects_tracked_ignored_files_and_leaks_without_echoing_secret(tmp_path):
    module = script('audit_public')
    subprocess.run(['git', 'init', '--quiet', str(tmp_path)], check=True)
    (tmp_path/'.gitignore').write_text('.env\n')
    (tmp_path/'.env').write_text('private setting\n')
    subprocess.run(['git', '-C', str(tmp_path), 'add', '-f', '.env'], check=True)
    secret = 'hf_'+'x'*30
    (tmp_path/'bad.txt').write_text('/home'+'/private-user/work\n'+secret+'\n')
    result = module.audit(tmp_path)
    assert not result['ok']
    reasons = {item['reason'] for item in result['issues']}
    assert {'already tracked despite ignore rule', 'personal home path', 'access token'} <= reasons
    assert secret not in json.dumps(result)


def test_all_public_profiles_load_without_calibration_or_personal_devices(monkeypatch):
    monkeypatch.setenv('SO101_USE_LOCAL_CONFIG', '0')
    for path in (ROOT/'config/runtime').glob('system*.yaml'):
        config = load_system(path)
        assert config['robot']['hardware_qualified'] is False
        for role in ('leader', 'follower'):
            assert 'YOUR_' in config['robot'][role]['port']
        assert all('YOUR_' in c['device'] for c in config['camera']['cameras'].values())

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from so101_core.policy_catalog import MODELS, require_method


def test_major_so101_models_and_all_method_capabilities_are_explicit():
    assert {'act', 'diffusion', 'smolvla', 'pi0', 'pi05', 'pi0_fast', 'groot', 'xvla'} <= MODELS.keys()
    for family in MODELS:
        for method in ('standard', 'vlash'):
            assert require_method(family, method)
    for family in ('flow', 'smolvla', 'pi0', 'pi05', 'groot', 'xvla'):
        for method in ('rtc', 'training_time_rtc'):
            assert require_method(family, method)
    assert require_method('diffusion', 'rtc')
    with pytest.raises(ValueError, match='global noise timestep'):
        require_method('diffusion', 'training_time_rtc')
    with pytest.raises(ValueError, match='discrete action tokens'):
        require_method('pi0_fast', 'rtc')


def test_cli_rejects_unsupported_pair_before_loading_data_or_models(tmp_path):
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]/'ros2_ws/src/so101_core')}
    command = [sys.executable, '-m', 'so101_core.methods_cli']
    result = subprocess.run(command+['models'], env=env, text=True, capture_output=True)
    assert result.returncode == 0
    assert len(json.loads(result.stdout)) == 9
    result = subprocess.run(command+['train', '--method', 'rtc', '--policy', 'act', '--dataset', 'missing',
        '--model-id', 'test', '--output', str(tmp_path/'new')], env=env, text=True, capture_output=True)
    assert result.returncode == 2 and 'CVAE' in result.stderr
    assert not (tmp_path/'new').exists()


def test_deploy_selects_registered_family_method_and_correction(tmp_path):
    root = Path(__file__).resolve().parents[1]
    folder = tmp_path/'models/demo'; folder.mkdir(parents=True)
    (folder/'deployment.json').write_text(json.dumps({'model_format': 'so101_lerobot_v1',
                                                     'training_config': {'method': 'vlash'}}))
    result = subprocess.run([sys.executable, str(root/'scripts/policy_runtime.py'), 'demo', 'mock',
        '--model-root', str(tmp_path/'models'), '--correction', '/workspace/data/correction', '--dry-run'],
        capture_output=True, text=True, check=True)
    command = json.loads(result.stdout)
    assert 'policy_backend:=lerobot' in command
    assert 'config:=/workspace/config/runtime/system.real30hz.640x480.vlash.a2c2.yaml' in command
    assert 'model_python:=/workspace/data/envs/policies/bin/python' in command
    saved = folder/'runtime/system.a2c2.yaml'
    saved.parent.mkdir(); saved.write_text('{}')
    (folder/'deployment.json').write_text(json.dumps({'model_format': 'so101_lerobot_v1',
        'training_config': {'method': 'vlash'}, 'runtime_profiles': {'a2c2': 'runtime/system.a2c2.yaml'}}))
    result = subprocess.run([sys.executable, str(root/'scripts/policy_runtime.py'), 'demo', 'mock',
        '--model-root', str(tmp_path/'models'), '--correction', '/workspace/data/correction', '--dry-run'],
        capture_output=True, text=True, check=True)
    assert 'config:='+str(saved) in json.loads(result.stdout)


@pytest.mark.parametrize('model_format,method', [('so101_act_v1', 'standard')]+[
    (family, method) for family in ('so101_flow_v1', 'so101_lerobot_v1')
    for method in ('standard', 'rtc', 'training_time_rtc', 'vlash')])
def test_deploy_smoothing_is_optional_for_all_registered_backends(tmp_path, model_format, method):
    root = Path(__file__).resolve().parents[1]
    folder = tmp_path/'models/demo'; folder.mkdir(parents=True)
    manifest = {'model_format': model_format, 'training_config': {'method': method}}
    path = folder/'deployment.json'; path.write_text(json.dumps(manifest))
    command = [sys.executable, str(root/'scripts/policy_runtime.py'), 'demo', 'mock',
               '--model-root', str(tmp_path/'models'), '--dry-run']
    normal = subprocess.run(command, capture_output=True, text=True, check=True)
    assert not any('action_smoothing:=' in part for part in json.loads(normal.stdout))
    enabled = subprocess.run(command+['--smoothing', 'three_point'], capture_output=True, text=True, check=True)
    assert 'action_smoothing:=three_point' in json.loads(enabled.stdout)
    if model_format != 'so101_act_v1':
        corrected = subprocess.run(command+['--smoothing', 'three_point', '--correction', '/tmp/correction'],
                                   capture_output=True, text=True, check=True)
        assert 'action_smoothing:=three_point' in json.loads(corrected.stdout)


@pytest.mark.parametrize('script,model', [('runtime.sh', None), ('deploy_policy.sh', 'demo')])
@pytest.mark.parametrize('backend', [None, 'real'])
def test_all_runtime_script_entrypoints_accept_smoothing_without_starting_docker(tmp_path, script, model, backend):
    root = Path(__file__).resolve().parents[1]
    command = [str(root/'scripts'/script)]
    if model:
        folder = tmp_path/'models'/model
        folder.mkdir(parents=True)
        (folder/'deployment.json').write_text(json.dumps({'model_format':'so101_flow_v1',
                                                         'training_config':{'method':'vlash'}}))
        command.append(model)
    if backend: command.append(backend)
    if model: command.extend(['--model-root', str(tmp_path/'models')])
    def launch_args(result):
        planned = json.loads(result.stdout)
        if model:
            # Follow the public wrapper through to runtime.sh's launch plan.
            result = subprocess.run(planned+['--dry-run'], capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        return planned
    raw = subprocess.run(command+['--dry-run'], capture_output=True, text=True, check=True)
    assert not any('action_smoothing:=' in arg for arg in launch_args(raw))
    enabled = subprocess.run(command+['--smoothing', 'three_point', '--dry-run'],
                             capture_output=True, text=True, check=True)
    assert 'action_smoothing:=three_point' in launch_args(enabled)
    assert 'backend:='+(backend or 'mock') in launch_args(enabled)
    invalid = subprocess.run(command+['--smoothing', 'unknown', '--dry-run'], capture_output=True, text=True)
    assert invalid.returncode == 2

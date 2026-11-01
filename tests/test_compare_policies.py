"""Keep the four-condition experiment balanced, reproducible and honest about missing data."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def comparison():
    spec = importlib.util.spec_from_file_location('compare_policies', ROOT/'scripts/compare_policies.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def fake_model(root, method, seed=42):
    folder = root/method
    (folder/'checkpoint').mkdir(parents=True); (folder/'runtime').mkdir()
    training = {'method': method, 'policy': 'smolvla', 'seed': seed, 'steps': 30000,
                'dataset_signature': 'same-dataset', 'train_episodes': [0, 1],
                'validation_episodes': [], 'training_frames': 100, 'batch_size': 16,
                'policy_overrides': {'train_expert_only': True}}
    bridge = {'objective': method, 'policy_family': 'smolvla', 'pretrained_source': 'example/base',
              'chunk_size': 30, 'joint_names': ['q', 'gripper'], 'cameras': ['top'],
              'image_shapes': {'top': [64, 64, 3]}, 'task': 'place object', 'fps': 30,
              'max_delay_steps': 15, 'denoise_steps': 10, 'action_semantics': 'absolute_joint_position_rad'}
    policy = {'inference_rate_hz': 30, 'action_chunk': {'chunk_size': 30, 'n_action_steps': 10,
              'scheduler': {'method': method, 'max_delay_steps': 15, 'max_inference_s': .6},
              'correction': {'method': 'none'}}}
    manifest = {'training_config': training, 'robot_hash': 'robot', 'calibration_sha256': {},
                'observation_hash': 'observation', 'synchronization': {}, 'units': 'rad',
                'checkpoint_sha256': {'normalization.json': 'same-statistics', 'model.safetensors': method},
                'runtime_profiles': {'none': 'runtime/system.yaml'}}
    for name, value in [('deployment.json', manifest), ('checkpoint/train_config.json', training),
                        ('checkpoint/so101_adapter.json', bridge), ('runtime/policy.yaml', policy),
                        ('runtime/system.yaml', {'files': {'policy': 'policy.yaml'}})]:
        (folder/name).write_text(json.dumps(value))
    (folder/'checkpoint/model.safetensors').write_text('Synthetic fixture, never loaded as weights')
    return folder


def test_order_has_all_four_conditions_in_every_block_and_reproduces(tmp_path, comparison):
    root = tmp_path/'models'; fake_model(root, 'vlash')
    for name in ('first', 'repeat'):
        status = comparison.prepare(tmp_path/name, 'standard', 'vlash', root,
                                    repetitions=5, seed=42, allow_pending=True)
        assert not status['ready'] and status['pending_methods'] == ['standard']
    first = comparison.read(tmp_path/'first/plan.json')['trials']
    assert first == comparison.read(tmp_path/'repeat/plan.json')['trials']
    for index in range(0, 20, 4):
        assert {r['condition'] for r in first[index:index+4]} == {r[0] for r in comparison.CONDITIONS}
    with pytest.raises(ValueError, match='Wait for both'):
        comparison.command(comparison.read(tmp_path/'first/plan.json'), 1, 'real')
    with pytest.raises(ValueError, match='Both registered'):
        comparison.record(tmp_path/'first', 1, 'success', {'duration_s': 5})
    assert not (tmp_path/'first/trials').exists()


def test_training_or_runtime_confounds_reject_the_comparison(tmp_path, comparison):
    root = tmp_path/'models'; fake_model(root, 'vlash'); standard = fake_model(root, 'standard', seed=7)
    with pytest.raises(ValueError, match='settings differ: training'):
        comparison.prepare(tmp_path/'bad', 'standard', 'vlash', root)
    assert not (tmp_path/'bad').exists()
    training = comparison.read(standard/'checkpoint/train_config.json'); training['seed'] = 42
    (standard/'checkpoint/train_config.json').write_text(json.dumps(training))
    manifest = comparison.read(standard/'deployment.json'); manifest['training_config'] = training
    (standard/'deployment.json').write_text(json.dumps(manifest))
    policy = comparison.read(standard/'runtime/policy.yaml'); policy['action_chunk']['n_action_steps'] = 5
    (standard/'runtime/policy.yaml').write_text(json.dumps(policy))
    with pytest.raises(ValueError, match='settings differ: runtime'):
        comparison.prepare(tmp_path/'bad', 'standard', 'vlash', root)


def test_absent_trials_and_measurements_are_not_reported_as_zero_performance(tmp_path, comparison):
    root = tmp_path/'models'; fake_model(root, 'standard'); fake_model(root, 'vlash')
    folder = tmp_path/'experiment'
    comparison.prepare(folder, 'standard', 'vlash', root, repetitions=3)
    plan = comparison.read(folder/'plan.json')
    selected = [t for t in plan['trials'] if t['condition'] == 'standard_none']
    comparison.record(folder, selected[0]['trial_id'], 'success', {'duration_s': 12},
                      notes='Private operator note', recording='/private/recording/file.mcap')
    comparison.record(folder, selected[1]['trial_id'], 'aborted', {'duration_s': 5})
    result = comparison.summarize(folder, tmp_path/'export')
    standard = result['conditions'][0]
    assert standard['attempts'] == 2 and standard['aborted'] == 1
    assert standard['success_rate'] == .5 and standard['unrecorded'] == 1
    assert standard['successful_duration_median_s'] == 12
    assert standard['measurements']['inference_p95_ms'] == {'median': None, 'measured_attempts': 0}
    assert not result['complete']
    assert result['conditions'][1]['success_rate'] is None
    assert result['model_evidence']['standard']['checkpoint_sha256']['model.safetensors'] == 'standard'
    exported = ''.join(p.read_text() for p in (tmp_path/'export').iterdir())
    assert 'Private operator note' not in exported and '/private/recording' not in exported
    ci = standard['success_rate_ci95']; assert ci[0] < .5 < ci[1]


def test_rejects_late_success_duplicate_results_and_protocol_changes(tmp_path, comparison):
    root = tmp_path/'models'; fake_model(root, 'standard'); fake_model(root, 'vlash')
    folder = tmp_path/'experiment'; comparison.prepare(folder, 'standard', 'vlash', root, repetitions=1)
    with pytest.raises(ValueError, match='time limit'):
        comparison.record(folder, 1, 'success', {'duration_s': 31})
    comparison.record(folder, 1, 'failure', {'duration_s': 30})
    with pytest.raises(FileExistsError):
        comparison.record(folder, 1, 'success', {'duration_s': 10})
    plan = comparison.read(folder/'plan.json'); plan['max_duration_s'] = 60
    (folder/'plan.json').write_text(json.dumps(plan))
    with pytest.raises(ValueError, match='protocol or settings changed'):
        comparison.record(folder, 2, 'success', {'duration_s': 10})
    with pytest.raises(ValueError, match='protocol differs'):
        comparison.summarize(folder, tmp_path/'export')


def test_all_four_commands_use_async_inference_and_never_enable_autonomy(tmp_path, comparison):
    root = tmp_path/'models'; fake_model(root, 'standard'); fake_model(root, 'vlash')
    comparison.prepare(tmp_path/'experiment', 'standard', 'vlash', root, repetitions=1)
    plan = comparison.read(tmp_path/'experiment/plan.json')
    for item in plan['trials']:
        command = comparison.command(plan, item['trial_id'], 'real')
        assert '--async-inference' in command
        assert command[command.index('--smoothing')+1] == item['smoothing']
        assert 'AUTONOMOUS' not in ' '.join(command)
    # Verify the deployment CLI carries the opt-in all the way to ROS launch.
    model = root/'standard'
    manifest = comparison.read(model/'deployment.json')
    manifest['model_format'] = 'so101_lerobot_v1'
    (model/'deployment.json').write_text(json.dumps(manifest))
    args = [sys.executable, str(ROOT/'scripts/policy_runtime.py'), 'standard', 'mock',
            '--model-root', str(root), '--async-inference', '--smoothing', 'three_point', '--dry-run']
    launch = json.loads(subprocess.check_output(args, text=True))
    assert 'asynchronous_inference:=true' in launch
    assert 'action_smoothing:=three_point' in launch


def test_wilson_interval_handles_extremes_without_false_certainty(comparison):
    assert comparison.wilson(0, 0) is None
    low, high = comparison.wilson(30, 30)
    assert .88 < low < 1 and high == pytest.approx(1)
    low, high = comparison.wilson(0, 30)
    assert low == pytest.approx(0) and 0 < high < .12


def test_native_flow_architecture_is_compared_for_synthetic_benchmarks(tmp_path, comparison):
    root = tmp_path/'models'
    for method in ('standard', 'vlash'):
        folder = fake_model(root, method)
        adapter = folder/'checkpoint/so101_adapter.json'
        config = comparison.read(adapter)
        config.update(format='so101_flow_v1', dim_model=16, n_heads=2, n_layers=1,
                      dropout=0., max_guidance_weight=5.)
        (folder/'checkpoint/config.json').write_text(json.dumps(config))
        adapter.unlink()
    assert comparison.prepare(tmp_path/'valid', 'standard', 'vlash', root)['ready']
    path = root/'vlash/checkpoint/config.json'
    config = comparison.read(path); config['dim_model'] = 32
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match='settings differ: model'):
        comparison.validate(comparison.read(tmp_path/'valid/plan.json'))

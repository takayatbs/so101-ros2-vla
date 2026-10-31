"""Actual LeRobot networks and processor serialization on synthetic SO101 data."""
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import numpy as np
import pytest

torch = pytest.importorskip('torch')
pytest.importorskip('lerobot.policies.factory')
import yaml

from so101_core.config import load_system, config_hash, calibration_hashes
from so101_core.dataset import DatasetWriter, FORMAT
from so101_core.lerobot_io import export_lerobot, open_frames
from so101_core.lerobot_policy import train_lerobot, LeRobotPolicy, processor_statistics, PolicySamples, load_weights_strict
from so101_core.method_training import prepare_correction, train_correction
from so101_core.flow import A2C2
from so101_core.synchronization import alignment_settings

pytestmark = [pytest.mark.ml, pytest.mark.skipif(os.environ.get('RUN_ML_TESTS') != '1', reason='Set RUN_ML_TESTS=1')]


@pytest.fixture(scope='module')
def model_data(tmp_path_factory):
    torch.set_num_threads(2)
    root = tmp_path_factory.mktemp('lerobot-models')
    config = root/'config'
    shutil.copytree(Path(__file__).resolve().parents[1]/'config', config)
    path = config/'observation/observation.yaml'
    obs = yaml.safe_load(path.read_text())
    for item in obs['preprocessing'].values():
        item['resize'].update(width=64, height=64)
    path.write_text(yaml.safe_dump(obs))
    for path in config.glob('policy/*.yaml'):
        value = yaml.safe_load(path.read_text()); value['device'] = 'cpu'; path.write_text(yaml.safe_dump(value))
    cfg = load_system(config/'runtime/system.real30hz.640x480.standard.yaml')
    native = root/'native'
    writer = DatasetWriter(native, 'local/models', 30, cfg['robot']['joints'],
                           {n: [64, 64, 3] for n in obs['cameras']})
    for ep in range(3):
        for frame in range(6):
            q = np.full(6, ep*.01+frame*.01, dtype=np.float32)
            writer.add_frame(q, q+.01, {n: np.full((64, 64, 3), 40+frame, dtype=np.uint8)
                                       for n in obs['cameras']}, ep+frame/30, 'move object')
        writer.save_episode(f'synthetic-{ep}')
    writer.finalize()
    (native/'provenance.json').write_text(json.dumps({'dataset_format_version': FORMAT, 'dataset_version': 'native',
        'observation_hash': config_hash(cfg['observation']), 'robot_hash': config_hash(cfg['robot']),
        'calibration_sha256': calibration_hashes(cfg['robot']), 'units': 'rad', 'joint_names': cfg['robot']['joints'],
        'synchronization_config': alignment_settings(cfg['dataset'])}))
    exported = export_lerobot(native, root/'lerobot', encoder='libx264')
    return root, config, exported


@pytest.mark.parametrize('family,method', [('act', 'standard'), ('act', 'vlash'),
                                         ('diffusion', 'standard'), ('diffusion', 'rtc'), ('diffusion', 'vlash')])
def test_train_register_reload_evaluate_and_a2c2(model_data, family, method):
    root, config, dataset = model_data
    overrides = {'pretrained_backbone_weights': None}
    if family == 'act':
        overrides.update(dim_model=32, n_heads=4, dim_feedforward=64, n_encoder_layers=1,
                         n_decoder_layers=1, n_vae_encoder_layers=1)
    else:
        overrides.update(down_dims=[32, 64], n_groups=4, diffusion_step_embed_dim=32,
                         num_train_timesteps=4, num_inference_steps=2, spatial_softmax_num_keypoints=4)
    settings = root/f'{family}-{method}.json'; settings.write_text(json.dumps(overrides))
    args = SimpleNamespace(policy=family, method=method, config=str(config/f'runtime/system.real30hz.640x480.{method}.yaml'),
        dataset=str(dataset), output=str(root/f'{family}-{method}'), model_id=f'{family}-{method}',
        steps=2, batch_size=2, device='cpu', seed=42, denoise_steps=2, pretrained=None, policy_config=str(settings))
    if family == 'act' and method == 'standard':
        args.steps, args.save_every, args.keep_checkpoints = 3, 1, 1
    model = train_lerobot(args)
    if family == 'act' and method == 'standard':
        kept = list((Path(args.output)/'checkpoints').glob('step-*'))
        assert len(kept) == 1 and kept[0].name == 'step-000002'
        assert LeRobotPolicy(kept[0], 'cpu').config['training_method'] == 'standard'
    manifest = json.loads((model/'deployment.json').read_text())
    saved = load_system(model/manifest['runtime_profiles']['none'])
    corrected_profile = load_system(model/manifest['runtime_profiles']['a2c2'])
    assert saved['policy']['model_id'] == f'{family}-{method}'
    assert saved['policy']['action_chunk']['scheduler']['method'] == method
    assert saved['policy']['action_chunk']['scheduler']['max_delay_steps'] == 3
    assert corrected_profile['policy']['action_chunk']['correction']['method'] == 'a2c2'
    policy = LeRobotPolicy(model/'checkpoint', 'cpu')
    ds = open_frames(dataset, 30); frame = ds[0]
    observation = {'observation.state': frame['state'],
                   **{'observation.images.'+n: v for n, v in frame['images'].items()}}
    result = policy.predict(observation, method)
    assert result.shape == (30, 6) and np.isfinite(result).all()
    if method == 'rtc':
        previous = np.tile(frame['state'], (8, 1))
        guided = policy.predict(observation, method, previous, 2)
        assert guided.shape == result.shape and np.isfinite(guided).all()
    assert policy.features(observation).shape == (1, policy.config['dim_model'])
    cache = prepare_correction(SimpleNamespace(model=str(model), dataset=str(dataset), device='cpu', seed=42,
        stride=3, max_samples=1000, output=str(root/f'cache-{family}-{method}')))
    correction = train_correction(SimpleNamespace(cache=str(cache), output=str(root/f'a2c2-{family}-{method}'),
        device='cpu', steps=2, batch_size=4, hidden=16, seed=42))
    corrected = A2C2(correction, policy).correct(observation, result[0], 0, 1)
    assert corrected.shape == (6,) and np.isfinite(corrected).all()
    policy.reset()


def test_vlash_temporal_sampling_keeps_image_and_fits_train_only_stats(model_data, monkeypatch):
    _, _, dataset = model_data
    ds = open_frames(dataset, 4, [1])
    monkeypatch.setattr(np.random, 'randint', lambda *args: 2)
    sample = PolicySamples(ds, 'vlash', 3)[0]
    np.testing.assert_allclose(sample['observation.state'], ds.tables[1]['state'][2])
    np.testing.assert_allclose(sample['action'][0], ds.tables[1]['action'][2])
    assert sample['observation.images.top'][0, 0, 0] == pytest.approx(ds[0]['images']['top'][0, 0, 0]/255)
    stats = processor_statistics(ds)
    np.testing.assert_allclose(stats['observation.state']['mean'], ds.tables[1]['state'].mean(0))


def test_checkpoint_loading_rejects_incomplete_weights_and_restores_only_tied_aliases(tmp_path):
    from safetensors.torch import save_file
    model = torch.nn.Module()
    model.first = torch.nn.Linear(2, 2, bias=False)
    model.second = model.first
    expected = torch.ones(2, 2)
    save_file({'first.weight': expected}, str(tmp_path/'model.safetensors'))
    load_weights_strict(model, tmp_path)
    torch.testing.assert_close(model.second.weight, expected)
    save_file({'wrong.weight': expected}, str(tmp_path/'model.safetensors'))
    with pytest.raises(RuntimeError, match='Missing key'):
        load_weights_strict(model, tmp_path)


def test_default_vla_delay_budget_and_explicit_overrides_do_not_change_source_config(model_data):
    from so101_core.method_training import training_system
    _, config, _ = model_data
    path = str(config/'runtime/system.real30hz.640x480.vlash.yaml')
    args = SimpleNamespace(config=path, policy='smolvla', auto_config=True)
    assert training_system(args)['policy']['action_chunk']['scheduler']['max_delay_steps'] == 15
    args.auto_config = False
    assert training_system(args)['policy']['action_chunk']['scheduler']['max_delay_steps'] == 3
    args.max_delay_steps, args.max_inference_s = 7, .5
    scheduler = training_system(args)['policy']['action_chunk']['scheduler']
    assert scheduler['max_delay_steps'] == 7 and scheduler['max_inference_s'] == .5
    assert load_system(path)['policy']['action_chunk']['scheduler']['max_delay_steps'] == 3
    args.validation_fraction = 0
    assert training_system(args)['training']['validation_fraction'] == 0


@pytest.mark.ros
@pytest.mark.parametrize('family,method,smoothing', [('act', 'vlash', 'none'),
    ('act', 'vlash', 'three_point'), ('act', 'standard', 'three_point'),
    ('diffusion', 'standard', 'three_point')])
def test_ros_lerobot_adapter_readiness_async_actions_and_mode_stop(model_data, monkeypatch, family, method, smoothing):
    import time
    rclpy = pytest.importorskip('rclpy')
    from rclpy.executors import SingleThreadedExecutor
    from sensor_msgs.msg import Image, JointState
    from std_msgs.msg import String, Bool
    from trajectory_msgs.msg import JointTrajectory
    from so101_core.ros_utils import STATE_QOS, COMMAND_QOS, LATCHED_QOS, joint_state
    from so101_policy.node import PolicyNode
    root, config, dataset = model_data
    tag=f'ros-{family}-{method}-{smoothing}'
    settings = root/(tag+'.json')
    overrides={'pretrained_backbone_weights': None}
    if family=='act':
        overrides.update(dim_model=32,n_heads=4,dim_feedforward=64,n_encoder_layers=1,
                         n_decoder_layers=1,n_vae_encoder_layers=1)
    else:
        overrides.update(down_dims=[32,64],n_groups=4,diffusion_step_embed_dim=32,
                         num_train_timesteps=4,num_inference_steps=2,spatial_softmax_num_keypoints=4)
    settings.write_text(json.dumps(overrides))
    train_lerobot(SimpleNamespace(policy=family, method=method,
        config=str(config/f'runtime/system.real30hz.640x480.{method}.yaml'), dataset=str(dataset),
        output=str(root/tag), model_id=tag, steps=2, batch_size=2, device='cpu',
        seed=42, denoise_steps=2, pretrained=None, policy_config=str(settings)))
    monkeypatch.setenv('ROS_DOMAIN_ID', '97')
    saved_profile = root/f'data/models/{tag}/runtime/system.yaml'
    rclpy.init(args=['--ros-args', '-p', f'config:={saved_profile}',
                    '-p', 'policy_backend:=lerobot', '-p', 'model_id:='+tag,
                    '-p', 'action_smoothing:='+smoothing])
    node = PolicyNode(); peer = rclpy.create_node('lerobot_adapter_test_peer')
    assert (node.engine.smoothing if node.engine else node.scheduler.smoothing)==smoothing
    executor = SingleThreadedExecutor(); executor.add_node(node); executor.add_node(peer)
    ready, actions = [], []
    peer.create_subscription(Bool, '/policy/ready', lambda m: ready.append(m.data), LATCHED_QOS)
    peer.create_subscription(JointTrajectory, '/policy/action', lambda m: actions.append(m), COMMAND_QOS)
    state = peer.create_publisher(JointState, '/hardware/follower/state', STATE_QOS)
    mode = peer.create_publisher(String, '/control_mode', LATCHED_QOS)
    cameras = [peer.create_publisher(Image, '/camera/'+n+'/image_raw', STATE_QOS) for n in ('top', 'arm')]
    def publish():
        value = joint_state(peer, node.joints, [.02]*6); state.publish(value)
        for pub in cameras:
            image = Image(height=64, width=64, encoding='rgb8', step=192, data=bytes([50])*(64*64*3))
            image.header = value.header; pub.publish(image)
    timer = peer.create_timer(1/30, publish)
    def until(predicate):
        end = time.monotonic()+8
        while not predicate() and time.monotonic() < end:
            executor.spin_once(timeout_sec=.01)
        assert predicate()
    try:
        mode.publish(String(data='IDLE')); until(lambda: bool(ready) and ready[-1])
        assert not actions
        mode.publish(String(data='AUTONOMOUS')); until(lambda: len(actions) >= 10)
        assert all(np.isfinite(m.points[0].positions).all() for m in actions)
        mode.publish(String(data='TELEOP')); until(lambda: node.mode == 'TELEOP')
        count = len(actions); end = time.monotonic()+.3
        while time.monotonic() < end:
            executor.spin_once(timeout_sec=.01)
        assert len(actions) == count
    finally:
        peer.destroy_timer(timer); executor.shutdown(); node.destroy_node(); peer.destroy_node(); rclpy.shutdown()

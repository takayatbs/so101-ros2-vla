"""LeRobot policy bridge. Robot-facing states and actions always remain radians.

LeRobot and its model extras are optional; importing the catalog/CLI needs neither.
Official processors own model normalization/tokenization. The bridge never uses
the native LeRobot motor normalization (-100..100, degrees, gripper percent).
"""
import hashlib
import json
import shutil
import time
from pathlib import Path

import numpy as np
import torch
from torch.nn import functional as F

from .policy_catalog import require_method

FORMAT = 'so101_lerobot_v1'
BRIDGE_FILE = 'so101_adapter.json'
FLOW_FAMILIES = ('smolvla', 'pi0', 'pi05', 'xvla', 'groot')


def flow_adapter(policy, family):
    if family == 'xvla':
        from .xvla_flow import XVLAFlow
        return XVLAFlow(policy)
    if family == 'groot':
        from .groot_flow import GrootFlow
        return GrootFlow(policy)
    from .lerobot_flow import LeRobotFlow
    return LeRobotFlow(policy, family)


def metadata(folder):
    folder = Path(folder)
    return json.loads((folder/(BRIDGE_FILE if (folder/BRIDGE_FILE).exists() else 'config.json')).read_text())


def _factory():
    try:
        from lerobot.policies.factory import get_policy_class, make_policy_config, make_pre_post_processors
        return get_policy_class, make_policy_config, make_pre_post_processors
    except ImportError as exc:
        raise RuntimeError('LeRobot model dependencies are missing. Run ./scripts/install_policies.sh MODEL; '
                           'see docs/methods/models.ja.md.') from exc


def feature_schema(info):
    from lerobot.configs.types import FeatureType, PolicyFeature
    inputs = {'observation.state': PolicyFeature(FeatureType.STATE, (len(info['joint_names']),))}
    for name, (h, w, c) in info['image_shapes'].items():
        inputs['observation.images.'+name] = PolicyFeature(FeatureType.VISUAL, (c, h, w))
    return inputs, {'action': PolicyFeature(FeatureType.ACTION, (len(info['joint_names']),))}


def load_weights_strict(policy, source, remap=False):
    """Never accept the upstream pi loaders' fallback to random weights."""
    from safetensors.torch import load_file
    source = str(source)
    if Path(source).is_dir():
        path = Path(source)/'model.safetensors'
    else:
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(source, filename='model.safetensors')
    state = load_file(str(path))
    if remap:
        state = policy._fix_pytorch_state_dict_keys(state, policy.config)
        state = {key if key.startswith('model.') else 'model.'+key: value for key, value in state.items()}
    # Public checkpoints can omit aliases of tied tensors. Restore only aliases
    # that point to the exact same parameter; every other missing key is fatal.
    expected = policy.state_dict()
    aliases = {value.data_ptr(): key for key, value in expected.items() if key in state}
    for key, value in expected.items():
        if key not in state and value.data_ptr() in aliases:
            state[key] = state[aliases[value.data_ptr()]]
    policy.load_state_dict(state, strict=True)


def processor_statistics(ds):
    """Fit every vector statistic on the training source episodes only."""
    result = {}
    for name, column in [('observation.state', 'state'), ('action', 'action')]:
        values = np.concatenate([table[column][table.get('valid', np.ones(len(table[column]), dtype=bool))]
                                 for table in ds.tables.values()])
        low, high = values.min(0), values.max(0)
        q01, q99 = np.quantile(values, [.01, .99], axis=0)
        result[name] = {key: torch.as_tensor(value, dtype=torch.float32) for key, value in {
            'mean': values.mean(0), 'std': np.maximum(values.std(0), 1e-6),
            'min': low, 'max': np.maximum(high, low+1e-6),
            'q01': q01, 'q99': np.maximum(q99, q01+1e-6)}.items()}
    # Official ResNet policies use ImageNet image normalization.
    for camera in ds.info['image_shapes']:
        result['observation.images.'+camera] = {
            'mean': torch.tensor([.485, .456, .406])[:, None, None],
            'std': torch.tensor([.229, .224, .225])[:, None, None],
            'min': torch.zeros(3, 1, 1), 'max': torch.ones(3, 1, 1)}
    return result


def create_policy(family, info, h, device, stats, pretrained=None, overrides=None, denoise_steps=None):
    spec = require_method(family, 'standard')
    get_class, make_config, make_processors = _factory()
    cls = get_class(family)
    source = pretrained or spec.pretrained
    if family == 'groot':
        cfg = make_config(family, base_model_path=source, embodiment_tag='new_embodiment', device=device)
    elif source:
        cfg = cls.config_class.from_pretrained(source)
    else:
        cfg = make_config(family)
    cfg.device = device
    cfg.input_features, cfg.output_features = feature_schema(info)
    cfg.n_obs_steps = 1
    # Ask for the entire chunk; the ROS scheduler owns the execution horizon.
    cfg.n_action_steps = h
    if family == 'diffusion':
        multiple = 2**len(cfg.down_dims)
        cfg.horizon = ((h+multiple-1)//multiple)*multiple
        cfg.do_mask_loss_for_padding = True
        cfg.drop_n_last_frames = 0
    else:
        cfg.chunk_size = h
    if hasattr(cfg, 'use_relative_actions'):
        cfg.use_relative_actions = False
    if hasattr(cfg, 'action_feature_names'):
        cfg.action_feature_names = info['joint_names']
    if family == 'xvla':
        cfg.action_mode = 'auto'  # Six joint positions, not end-effector poses.
    if family == 'smolvla':
        # A complete SmolVLA checkpoint already includes the VLM weights.
        cfg.load_vlm_weights = False
    step_field = {'diffusion': 'num_inference_steps', 'smolvla': 'num_steps', 'pi0': 'num_inference_steps',
                  'pi05': 'num_inference_steps', 'xvla': 'num_denoising_steps', 'groot': 'num_inference_timesteps'}
    if denoise_steps is not None and family in step_field:
        setattr(cfg, step_field[family], denoise_steps)
    for key, value in (overrides or {}).items():
        if key in ('input_features', 'output_features', 'device', 'chunk_size', 'n_action_steps',
                   'n_obs_steps', 'horizon', 'use_relative_actions', 'action_mode',
                   'adapt_to_pi_aloha', 'use_delta_joint_actions_aloha'):
            raise ValueError('The SO101 bridge owns policy setting: '+key)
        if not hasattr(cfg, key):
            raise ValueError('Unknown model config option: '+key)
        setattr(cfg, key, value)
    if family in step_field and getattr(cfg, step_field[family]) is not None and getattr(cfg, step_field[family]) < 1:
        raise ValueError('Denoising steps must be positive')
    cfg.validate_features()
    # Raw NVIDIA weights are loaded by GrootPolicy.__init__, not LeRobot HubMixin.
    if source and family in ('pi0', 'pi05', 'pi0_fast'):
        policy = cls(cfg)
        load_weights_strict(policy, source, remap=True)
    else:
        policy = cls.from_pretrained(source, config=cfg, strict=True) if source and family != 'groot' else cls(cfg)
    policy = policy.to(device)
    processors = make_processors(cfg, dataset_stats=stats)
    return policy, processors


class PolicySamples(torch.utils.data.Dataset):
    def __init__(self, frames, method, max_delay, horizon=None):
        self.frames, self.method, self.max_delay = frames, method, max_delay
        self.horizon = horizon or frames.chunk_size

    def __len__(self):
        return len(self.frames)

    def __getitem__(self, index):
        from .method_training import run_end
        ds = self.frames
        frame = ds[index]
        ei, fi = ds.indices[index]; table = ds.tables[ei]
        end = run_end(table, fi)
        shift = np.random.randint(min(self.max_delay, end-fi-1)+1) if self.method == 'vlash' else 0
        indices = np.arange(fi+shift, fi+shift+self.horizon)
        result = {
            'observation.state': torch.from_numpy(table['state'][fi+shift].copy()),
            'action': torch.from_numpy(table['action'][np.minimum(indices, end-1)].copy()),
            'action_is_pad': torch.from_numpy(indices >= end),
            'task': frame.get('task', ds.info['episodes'][0]['task']),
        }
        result.update({'observation.images.'+n: torch.from_numpy(v.copy()).permute(2, 0, 1).float()/255
                       for n, v in frame['images'].items()})
        return result


def prepare_batch(preprocessor, raw, family):
    result = preprocessor(raw)
    if family == 'diffusion' and result['observation.state'].ndim == 2:
        result['observation.state'] = result['observation.state'][:, None]
    return result


class LeRobotPolicy:
    def __init__(self, checkpoint, device='cpu'):
        self.folder, self.device = Path(checkpoint), device
        self.config = metadata(self.folder)
        if self.config.get('format') != FORMAT:
            raise ValueError('SO101 LeRobot adapter checkpoint required')
        self.family = self.config['policy_family']
        require_method(self.family, self.config['training_method'])
        get_class, _, make_processors = _factory()
        cls = get_class(self.family)
        cfg = cls.config_class.from_pretrained(self.folder)
        cfg.device = device
        if self.family == 'smolvla':
            cfg.load_vlm_weights = False
        self.policy = cls(cfg)
        load_weights_strict(self.policy, self.folder)
        self.policy = self.policy.to(device).eval()
        self.preprocessor, self.postprocessor = make_processors(
            cfg, pretrained_path=str(self.folder),
            preprocessor_overrides={'device_processor': {'device': device}})
        for p in self.policy.parameters():
            p.requires_grad_(False)
        self.statistics = json.loads((self.folder/'normalization.json').read_text())
        self.stats = {k: torch.tensor(v, device=device, dtype=torch.float32) for k, v in self.statistics.items()}
        for key, values in self.stats.items():
            if values.shape != (len(self.config['joint_names']),) or not torch.isfinite(values).all() or (
                    key.endswith('_std') and (values <= 0).any()):
                raise ValueError('Invalid SO101 normalization statistics')
        digest = hashlib.sha256()
        for path in sorted(self.folder.rglob('*')):
            if path.is_file() and path.name not in ('train_config.json', 'metrics.json', 'evaluation.json', 'README.md'):
                digest.update(str(path.relative_to(self.folder)).encode())
                with path.open('rb') as stream:
                    for part in iter(lambda: stream.read(1024*1024), b''):
                        digest.update(part)
        self.fingerprint = digest.hexdigest()
        self.flow = None
        if self.family in FLOW_FAMILIES:
            self.flow = flow_adapter(self.policy, self.family)

    def reset(self):
        self.policy.reset()
        self.preprocessor.reset()
        self.postprocessor.reset()

    def _raw(self, observation):
        state = np.asarray(observation['observation.state'], dtype=np.float32)
        if state.shape != (len(self.config['joint_names']),) or not np.isfinite(state).all():
            raise ValueError('Invalid policy joint state')
        raw = {'observation.state': torch.from_numpy(state.copy())[None], 'task': [self.config['task']]}
        for name, shape in self.config['image_shapes'].items():
            value = np.asarray(observation['observation.images.'+name])
            if value.dtype != np.uint8 or list(value.shape) != shape:
                raise ValueError('Policy image shape/dtype mismatch: '+name)
            raw['observation.images.'+name] = torch.from_numpy(value.copy()).permute(2, 0, 1)[None].float()/255
        return raw

    def features(self, observation):
        """Small latest-observation descriptor for the separate A2C2 residual head.

        Deliberately independent of the large VLM and its mutable processors, so
        the scheduler can compute it while base inference runs in its worker.
        """
        raw = self._raw(observation)
        state = raw['observation.state'].to(self.device)
        parts = [(state-self.stats['state_mean'])/self.stats['state_std']]
        for name in self.config['cameras']:
            image = raw['observation.images.'+name].to(self.device)
            parts.append(F.adaptive_avg_pool2d(image, (8, 8)).flatten(1))
        return torch.cat(parts, -1)

    def predict(self, observation, method='standard', previous=None, delay=0):
        require_method(self.family, method)
        objective = {'rtc': 'standard', 'training_time_rtc': 'training_time_rtc', 'vlash': 'vlash'}
        if method in objective and self.config['objective'] != objective[method]:
            raise ValueError('Method differs from checkpoint training objective')
        raw = self._raw(observation)
        if previous is not None and method in ('rtc', 'training_time_rtc'):
            raw['action'] = torch.as_tensor(np.asarray(previous).copy(), dtype=torch.float32)[None]
        batch = prepare_batch(self.preprocessor, raw, self.family)
        if self.flow is not None:
            prefix = batch.get('action')
            if prefix is not None and previous is not None:
                prefix = prefix[:, :len(previous)]
            result = self.flow.sample(batch, self.config['denoise_steps'], method, prefix, delay)
        elif self.family == 'diffusion' and method == 'rtc':
            from .diffusion_rtc import predict_diffusion_rtc
            result = predict_diffusion_rtc(self.policy, batch, batch.get('action'), delay)
        else:
            with torch.no_grad():
                result = self.policy.predict_action_chunk(batch)
        with torch.no_grad():
            result = self.postprocessor(result)
        result = result[0, :self.config['chunk_size'], :len(self.config['joint_names'])].detach().float().cpu().numpy()
        if result.shape != (self.config['chunk_size'], len(self.config['joint_names'])) or not np.isfinite(result).all():
            raise ValueError('Invalid LeRobot action chunk')
        return result


def save_training_checkpoint(folder, policy, pre, post, bridge, training, stats, metrics):
    from safetensors.torch import save_file
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    policy.config.save_pretrained(folder)
    save_file({k: v.detach().cpu().contiguous().clone() for k, v in policy.state_dict().items()},
              str(folder/'model.safetensors'))
    pre.save_pretrained(folder); post.save_pretrained(folder)
    for name, value in [(BRIDGE_FILE, bridge), ('normalization.json', stats),
                        ('train_config.json', training), ('metrics.json', metrics)]:
        (folder/name).write_text(json.dumps(value, indent=2))


def train_lerobot(args):
    from torch.utils.data import DataLoader
    from .config import resolve
    from .lerobot_io import open_frames
    from .method_training import validate_source, split_episodes, dataset_signature, evaluate_model, training_system
    from .model_registry import register

    spec = require_method(args.policy, args.method)
    config = training_system(args)
    validate_source(args.dataset, config)
    if config['policy']['action_chunk']['scheduler']['method'] != args.method:
        raise ValueError('Use a System YAML for the selected method')
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(output)
    model_root = resolve(config['policy'], config['policy']['model_root']).resolve()
    target = (model_root/args.model_id).resolve()
    if not target.is_relative_to(model_root) or target == model_root or target.exists():
        raise ValueError('Choose a new model_id under model_root')
    if args.steps < 1 or args.batch_size < 1 or args.denoise_steps < 1:
        raise ValueError('steps, batch-size and denoise-steps must be positive')
    np.random.seed(args.seed); torch.manual_seed(args.seed)
    h = config['policy']['action_chunk']['chunk_size']
    full = open_frames(args.dataset, h)
    fps = config['policy']['inference_rate_hz']
    if full.info['fps'] != fps:
        raise ValueError('Policy execution FPS must match dataset FPS')
    delay = config['policy']['action_chunk']['scheduler'].get('max_delay_steps', 3)
    if not 0 <= delay < h-config['policy']['action_chunk']['n_action_steps']:
        raise ValueError('Chunk must cover execution horizon and delay')
    train_ids, val_ids = split_episodes(full, config['training']['validation_fraction'], args.seed)
    ds = open_frames(args.dataset, h, train_ids)
    cache_gb = getattr(args, 'image_cache_gb', 0)
    if cache_gb:
        from .video_storage import VideoReader
        reader = VideoReader(max_open=16, cache_bytes=int(cache_gb*1024**3))
        if hasattr(ds, 'video_reader'):
            ds.video_reader = reader
        else:
            ds.reader.close(); ds.reader = reader
    tasks = {e['task'] for e in full.info['episodes']}
    if len(tasks) != 1:
        raise ValueError('This SO101 runtime currently supports one task per registered model')
    overrides = json.loads(Path(args.policy_config).read_text()) if args.policy_config else {}
    policy, (pre, post) = create_policy(args.policy, ds.info, h, args.device,
                                       processor_statistics(ds), args.pretrained, overrides, args.denoise_steps)
    if getattr(policy.config, 'adapt_to_pi_aloha', False) or getattr(policy.config, 'use_delta_joint_actions_aloha', False):
        raise ValueError('SO101 adapter requires absolute joint radians, not Aloha action transforms')
    flow = None
    if args.policy in FLOW_FAMILIES:
        flow = flow_adapter(policy, args.policy)
    cfg = policy.config
    step_field = {'diffusion':'num_inference_steps', 'smolvla':'num_steps', 'pi0':'num_inference_steps',
                  'pi05':'num_inference_steps', 'xvla':'num_denoising_steps', 'groot':'num_inference_timesteps'}
    effective_steps = getattr(cfg, step_field[args.policy]) if args.policy in step_field else args.denoise_steps
    bridge = {'format': FORMAT, 'policy_family': args.policy, 'pretrained_source': args.pretrained or spec.pretrained,
              'objective': 'standard' if args.method in ('standard', 'rtc') else args.method,
              'training_method': args.method, 'chunk_size': h, 'joint_names': ds.info['joint_names'],
              'cameras': list(ds.info['image_shapes']), 'image_shapes': ds.info['image_shapes'],
              'task': next(iter(tasks)), 'fps': fps, 'max_delay_steps': delay, 'denoise_steps': effective_steps,
              'dim_model': len(ds.info['joint_names'])+192*len(ds.info['image_shapes']),
              'correction_features': 'normalized-state-and-8x8-pooled-rgb-v1',
              'action_semantics': 'absolute_joint_position_rad'}
    training = {'seed': args.seed, 'method': args.method, 'policy': args.policy, 'steps': args.steps,
                'dataset_root': str(Path(args.dataset).resolve()), 'dataset_signature': dataset_signature(args.dataset),
                'train_episodes': train_ids, 'validation_episodes': val_ids, 'training_frames': len(ds),
                'batch_size': args.batch_size, 'image_cache_gb': cache_gb,
                'backbone': args.policy, 'policy_overrides': overrides,
                'validation_status': 'not evaluated' if val_ids else 'no held-out source episodes'}
    stats = ds.statistics()
    loader = DataLoader(PolicySamples(ds, args.method, delay, getattr(cfg, 'horizon', h)),
                        batch_size=args.batch_size, shuffle=True, generator=torch.Generator().manual_seed(args.seed))
    # Preserve the model's differential parameter groups and optimizer preset.
    optimizer = cfg.get_optimizer_preset().build(policy.get_optim_params())
    scheduler_cfg = cfg.get_scheduler_preset()
    scheduler = scheduler_cfg.build(optimizer, args.steps) if scheduler_cfg is not None else None
    policy.train()
    output.mkdir(parents=True, exist_ok=False)
    (output/'train_config.json').write_text(json.dumps(training, indent=2))
    cfg.save_pretrained(output/'initial_policy_config')
    save_every = getattr(args, 'save_every', 1000)
    keep = getattr(args, 'keep_checkpoints', 2)
    started = time.monotonic()
    iterator = iter(loader)
    with (output/'metrics.jsonl').open('x') as log:
        for step in range(1, args.steps+1):
            try:
                raw = next(iterator)
            except StopIteration:
                iterator = iter(loader); raw = next(iterator)
            batch = prepare_batch(pre, raw, args.policy)
            if flow:
                loss = flow.loss(batch, delay, args.method == 'training_time_rtc')
            else:
                loss, _ = policy(batch)
            if not torch.isfinite(loss):
                raise ValueError('Nonfinite training loss')
            optimizer.zero_grad(set_to_none=True); loss.backward()
            torch.nn.utils.clip_grad_norm_(policy.parameters(), 1., error_if_nonfinite=True)
            optimizer.step()
            if scheduler is not None:
                scheduler.step()
            elapsed = time.monotonic()-started
            metrics = {'step': step, 'total_steps': args.steps, 'loss': float(loss.detach()),
                       'policy': args.policy, 'method': args.method, 'learning_rate': optimizer.param_groups[0]['lr'],
                       'elapsed_s': elapsed, 'steps_per_second': step/max(elapsed, 1e-9),
                       'eta_s': elapsed/step*(args.steps-step)}
            log.write(json.dumps(metrics)+'\n')
            if step == 1 or step % 100 == 0 or step == args.steps:
                log.flush()
                progress = output/'progress.json.tmp'
                progress.write_text(json.dumps(metrics, indent=2)); progress.replace(output/'progress.json')
                print(json.dumps(metrics), flush=True)
            if step % save_every == 0 and step < args.steps:
                checkpoints = output/'checkpoints'
                partial = checkpoints/f'.step-{step:06d}.partial'
                save_training_checkpoint(partial, policy, pre, post, bridge, training, stats, metrics)
                partial.rename(checkpoints/f'step-{step:06d}')
                for old in sorted(checkpoints.glob('step-*'))[:-keep]:
                    shutil.rmtree(old)
                print(json.dumps({'checkpoint_step':step, 'path':str(checkpoints/f'step-{step:06d}')}), flush=True)
    checkpoint = output/'checkpoint'
    save_training_checkpoint(checkpoint, policy, pre, post, bridge, training, stats, metrics)
    # Free optimizer buffers before loading the independently serialized policy.
    del optimizer, scheduler, loss, batch, raw, flow, policy, pre, post
    if args.device.startswith('cuda'):
        torch.cuda.empty_cache()
    if val_ids:
        from types import SimpleNamespace
        evaluation = evaluate_model(SimpleNamespace(model=str(checkpoint), dataset=args.dataset, device=args.device,
            output=str(output/'evaluation.json'), samples=32, delay_steps=delay, correction=None))
        metrics['validation_l1_rad'] = evaluation['mean_absolute_error_rad']
        training['validation_status'] = 'held-out source episodes; offline teacher-forced delay evaluation'
        (checkpoint/'metrics.json').write_text(json.dumps(metrics, indent=2))
        (checkpoint/'train_config.json').write_text(json.dumps(training, indent=2))
    target = register(checkpoint, args.dataset, args.model_id, config)
    print('Registered:', target, flush=True)
    return target

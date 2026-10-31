"""Train flow backbones / A2C2 on time-indexed demonstrations, with episode splits."""
import hashlib
import json
import random
import time
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import torch
from torch.utils.data import Dataset,DataLoader
from safetensors.torch import save_file
from .config import load_system,config_hash,calibration_hashes
from .converter import sha256_file
from .lerobot_io import open_frames
from .flow import FlowModel,FlowPolicy,CorrectionHead,A2C2,FORMAT,flow_loss
from .model_registry import register


def dataset_signature(root):
    root = Path(root)
    digest = hashlib.sha256()
    paths = [p for p in root.rglob('*') if p.is_file() and (p.suffix in ('.parquet','.npz') or p.name in
             ('dataset.json','info.json','so101_bridge.json','provenance.json'))]
    for p in sorted(paths):
        digest.update(str(p.relative_to(root)).encode());digest.update(sha256_file(p).encode())
    return digest.hexdigest()


def split_episodes(ds, fraction, seed):
    groups = {}
    for e in ds.info['episodes']:
        groups.setdefault(e.get('source_episode_id',e.get('episode_id')),[]).append(e['index'])
    names = list(groups);random.Random(seed).shuffle(names)
    nv = min(len(names)-1,max(1,round(len(names)*fraction))) if len(names)>1 and fraction>0 else 0
    val = [i for n in names[:nv] for i in groups[n]]
    train = [i for n in names[nv:] for i in groups[n]]
    return train,val


def run_end(table, frame):
    bad = np.flatnonzero(~table['valid'][frame:])
    return frame+int(bad[0]) if len(bad) else len(table['state'])


def observation(frame):
    return {'observation.state':frame['state'],**{'observation.images.'+n:v for n,v in frame['images'].items()}}


class TemporalSamples(Dataset):
    def __init__(self, ds, stats, method, max_delay, seed=0):
        self.ds,self.method,self.max_delay,self.seed = ds,method,max_delay,seed
        self.stats = {k:torch.tensor(v,dtype=torch.float32) for k,v in stats.items()}

    def __len__(self):return len(self.ds)

    def __getitem__(self, index):
        frame = self.ds[index]
        ei,fi = self.ds.indices[index];table = self.ds.tables[ei]
        offset = 0
        if self.method == 'vlash':
            offset = int(np.random.randint(0,min(self.max_delay,run_end(table,fi)-fi-1)+1))
        end = run_end(table,fi)
        indices = np.arange(fi+offset,fi+offset+self.ds.chunk_size)
        return {'state':(torch.from_numpy(table['state'][fi+offset].copy())-self.stats['state_mean'])/self.stats['state_std'],
                'action':(torch.from_numpy(table['action'][np.minimum(indices,end-1)].copy())-self.stats['action_mean'])/self.stats['action_std'],
                'padding':torch.from_numpy(indices>=end),
                'images':{n:torch.from_numpy(v.copy()).permute(2,0,1).float()/255 for n,v in frame['images'].items()},
                'offset':offset}


def device_batch(batch, device):
    return {k:{n:v.to(device) for n,v in value.items()} if isinstance(value,dict) else value.to(device) for k,value in batch.items()}


def validate_source(root, config):
    prov = json.loads((Path(root)/'provenance.json').read_text())
    for key,expected in [('observation_hash',config_hash(config['observation'])),('robot_hash',config_hash(config['robot'])),
                         ('joint_names',config['robot']['joints']),('units','rad'),('calibration_sha256',calibration_hashes(config['robot']))]:
        if prov.get(key) != expected:raise ValueError('Dataset/runtime mismatch: '+key)
    return prov


def training_system(args):
    config = load_system(args.config)
    if getattr(args, 'validation_fraction', None) is not None:
        config['training']['validation_fraction'] = args.validation_fraction
    scheduler = config['policy']['action_chunk']['scheduler']
    if getattr(args, 'auto_config', False) and getattr(args, 'policy', 'flow') != 'flow':
        scheduler.update(max_delay_steps=15, max_inference_s=.6)
    for key in ('max_delay_steps', 'max_inference_s'):
        value = getattr(args, key, None)
        if value is not None:
            scheduler[key] = value
    return config


def train_flow(args):
    if min(args.steps,args.batch_size,args.dim_model,args.denoise_steps)<1 or args.dim_model%args.n_heads:
        raise ValueError('Invalid training dimensions/steps')
    config = training_system(args);validate_source(args.dataset,config)
    method = args.method
    if config['policy']['action_chunk']['scheduler']['method'] != method:
        raise ValueError('Use a System YAML for the selected method')
    random.seed(args.seed);np.random.seed(args.seed);torch.manual_seed(args.seed)
    full = open_frames(args.dataset,config['policy']['action_chunk']['chunk_size'])
    if full.info['fps'] != config['policy']['inference_rate_hz']:
        raise ValueError('Policy execution FPS must match dataset FPS')
    if len({e['task'] for e in full.info['episodes']}) != 1:raise ValueError('This compact backbone supports one task per model')
    train_ids,val_ids = split_episodes(full,config['training']['validation_fraction'],args.seed)
    ds = open_frames(args.dataset,full.chunk_size,train_ids);stats = ds.statistics()
    max_delay = config['policy']['action_chunk']['scheduler'].get('max_delay_steps',3)
    if not 0<=max_delay<full.chunk_size-config['policy']['action_chunk']['n_action_steps']:
        raise ValueError('Chunk must cover execution horizon and delay')
    loader = DataLoader(TemporalSamples(ds,stats,method,max_delay),batch_size=args.batch_size,shuffle=True,
                        generator=torch.Generator().manual_seed(args.seed))
    objective = 'standard' if method in ('rtc','standard') else method
    model_config = {'format':FORMAT,'objective':objective,'training_method':method,
        'chunk_size':full.chunk_size,'joint_names':full.info['joint_names'],'cameras':list(full.info['image_shapes']),
        'image_shapes':full.info['image_shapes'],'task':full.info['episodes'][0]['task'],'fps':full.info['fps'],
        'dim_model':args.dim_model,'n_heads':args.n_heads,'n_layers':args.n_layers,'denoise_steps':args.denoise_steps,
        'max_delay_steps':max_delay,'max_guidance_weight':5.,'dropout':0.,'action_semantics':'absolute_joint_position_rad'}
    model = FlowModel(model_config).to(args.device)
    optimizer = torch.optim.AdamW(model.parameters(),lr=config['training']['learning_rate'])
    output = Path(args.output)
    output.mkdir(parents=True,exist_ok=False)
    training = {'seed':args.seed,'method':method,'dataset_root':str(Path(args.dataset).resolve()),
        'dataset_signature':dataset_signature(args.dataset),'train_episodes':train_ids,'validation_episodes':val_ids,
        'training_source_groups':sorted({e.get('source_episode_id',e.get('episode_id')) for e in full.info['episodes'] if e['index'] in train_ids}),
        'steps':args.steps,'backbone':'compact SO101 image-conditioned flow; not a foundation VLA',
        'validation_status':'not evaluated' if val_ids else 'no held-out source episodes'}
    iterator = iter(loader)
    with (output/'metrics.jsonl').open('x') as log:
        for step in range(1,args.steps+1):
            try:batch = next(iterator)
            except StopIteration:iterator=iter(loader);batch=next(iterator)
            batch = device_batch(batch,args.device)
            delays = torch.randint(0,max_delay+1,(len(batch['state']),),device=args.device) if objective=='training_time_rtc' else None
            loss = flow_loss(model,batch['state'],batch['images'],batch['action'],batch['padding'],delays)
            if not torch.isfinite(loss):raise ValueError('Nonfinite flow loss')
            optimizer.zero_grad(set_to_none=True);loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(),1.,error_if_nonfinite=True);optimizer.step()
            metrics = {'step':step,'flow_matching_loss':float(loss.detach())}
            log.write(json.dumps(metrics)+'\n')
            if step==1 or step%100==0 or step==args.steps:print(json.dumps(metrics),flush=True)
    checkpoint = output/'checkpoint';checkpoint.mkdir()
    save_file({k:v.detach().cpu().contiguous() for k,v in model.state_dict().items()},str(checkpoint/'model.safetensors'))
    for name,value in [('config.json',model_config),('normalization.json',stats),('train_config.json',training),('metrics.json',metrics)]:
        (checkpoint/name).write_text(json.dumps(value,indent=2))
    if val_ids:
        evaluation=evaluate_model(SimpleNamespace(model=str(checkpoint),dataset=args.dataset,device=args.device,
            output=str(output/'evaluation.json'),samples=32,delay_steps=max_delay,correction=None))
        metrics.update(validation_l1_rad=evaluation['mean_absolute_error_rad'],validation_protocol=evaluation['protocol'])
        training['validation_status']='held-out source episodes; offline teacher-forced delay evaluation'
        (checkpoint/'metrics.json').write_text(json.dumps(metrics,indent=2))
        (checkpoint/'train_config.json').write_text(json.dumps(training,indent=2))
    path = register(checkpoint,args.dataset,args.model_id,config)
    print('Registered:',path,flush=True)
    return path


def checkpoint_folder(path):
    path = Path(path)
    return path/'checkpoint' if (path/'checkpoint').is_dir() else path


def load_method_policy(path, device='cpu'):
    folder = checkpoint_folder(path)
    if (folder/'so101_adapter.json').is_file():
        from .lerobot_policy import LeRobotPolicy
        return LeRobotPolicy(folder, device)
    return FlowPolicy(folder, device)


def prepare_correction(args):
    policy = load_method_policy(args.model,args.device)
    ds = open_frames(args.dataset,policy.config['chunk_size'])
    training = json.loads((policy.folder/'train_config.json').read_text())
    if dataset_signature(args.dataset) != training['dataset_signature']:
        raise ValueError('A2C2 requires the exact base training dataset and split')
    method = policy.config['training_method'];h=policy.config['chunk_size']
    max_delay = getattr(args,'max_delay_steps',None)
    max_delay = policy.config['max_delay_steps'] if max_delay is None else max_delay
    if not 0<=max_delay<h or (method in ('training_time_rtc','vlash') and max_delay>policy.config['max_delay_steps']):
        raise ValueError('Correction delay range is incompatible with the base model')
    lookup = {pair:i for i,pair in enumerate(ds.indices)}
    columns = {k:[] for k in ('features','base','target','position','delay','episode')}
    rng = np.random.default_rng(args.seed)
    # Features for current observations are computed once per episode/frame.
    features = {}
    previous_episode = None
    for origin in range(0,len(ds),args.stride):
        ei,fi = ds.indices[origin];table=ds.tables[ei];end=run_end(table,fi)
        if ei != previous_episode:features={};previous_episode=ei
        frame = ds[origin];obs=observation(frame)
        delay = int(rng.integers(0,min(max_delay,end-fi-1)+1))
        offset = delay if method=='vlash' else 0
        prefix = None
        if method=='vlash':obs['observation.state']=table['state'][fi+delay].copy()
        elif method in ('rtc','training_time_rtc'):prefix=table['action'][fi:min(end,fi+h)].copy()
        predicted = policy.predict(obs,method,prefix,delay if prefix is not None else 0)
        # Already-committed prefix tokens do not train the correction head.
        for k in range(delay if prefix is not None else 0,min(h,end-fi-offset)):
            target_frame=fi+offset+k
            if target_frame not in features:
                latest=ds[lookup[ei,target_frame]]
                features[target_frame]=policy.features(observation(latest))[0].cpu().numpy()
            columns['features'].append(features[target_frame])
            columns['base'].append((predicted[k]-np.asarray(policy.statistics['action_mean']))/np.asarray(policy.statistics['action_std']))
            columns['target'].append((table['action'][target_frame]-predicted[k])/np.asarray(policy.statistics['action_std']))
            columns['position'].append(k/h);columns['delay'].append(delay/h);columns['episode'].append(ei)
            if len(columns['base'])>args.max_samples:
                raise MemoryError('Correction cache too large; increase --stride or --max-samples')
    if not columns['base']:raise ValueError('No A2C2 samples')
    output=Path(args.output);output.mkdir(parents=True,exist_ok=False)
    np.savez_compressed(output/'samples.npz',**{k:np.asarray(v,dtype=np.int64 if k=='episode' else np.float32) for k,v in columns.items()})
    meta={'format':'so101_a2c2_cache_v1','base_checkpoint':str(policy.folder.resolve()),'base_fingerprint':policy.fingerprint,
          'dataset_signature':training['dataset_signature'],'method':method,'train_episodes':training['train_episodes'],
          'validation_episodes':training['validation_episodes'],'features':policy.config['dim_model'],
          'max_delay_steps':max_delay,
          'joints':len(policy.config['joint_names']),'samples':len(columns['base']),'seed':args.seed,
          'conditioning':'demonstration-prefix offline augmentation; live closed-loop rollout evaluation still required'}
    (output/'cache.json').write_text(json.dumps(meta,indent=2))
    print(json.dumps(meta,indent=2),flush=True)
    return output


def evaluate_model(args):
    from .action_smoothing import ActionSmoother
    policy=load_method_policy(args.model,args.device)
    training=json.loads((policy.folder/'train_config.json').read_text())
    full=open_frames(args.dataset,policy.config['chunk_size'])
    if full.info['joint_names'] != policy.config['joint_names'] or full.info['image_shapes'] != policy.config['image_shapes']:
        raise ValueError('Evaluation schema differs from model')
    same=dataset_signature(args.dataset)==training['dataset_signature']
    ids=training['validation_episodes'] if same else list(full.tables)
    if not ids:raise ValueError('No held-out source episodes. Collect at least two source episodes or supply a separate evaluation dataset.')
    ds=open_frames(args.dataset,policy.config['chunk_size'],ids)
    lookup={pair:i for i,pair in enumerate(ds.indices)}
    method=policy.config['training_method'];h=policy.config['chunk_size']
    correction=A2C2(args.correction,policy) if getattr(args,'correction',None) else None
    if correction and correction.config['method']!=method:raise ValueError('Correction scheduler mismatch')
    smoothing = getattr(args, 'smoothing', 'none')
    smoother = ActionSmoother(smoothing, policy.config['joint_names'])
    total,count,latencies=0.,0,[]
    for origin in np.linspace(0,len(ds)-1,min(args.samples,len(ds)),dtype=int):
        ei,fi=ds.indices[int(origin)];table=ds.tables[ei];end=run_end(table,fi)
        delay=min(args.delay_steps,end-fi-1)
        if delay<0 or (method in ('vlash','training_time_rtc') and delay>policy.config['max_delay_steps']):
            raise ValueError('Evaluation delay is outside training range')
        frame=ds[int(origin)];obs=observation(frame);offset=delay if method=='vlash' else 0
        prefix=None
        if method=='vlash':obs['observation.state']=table['state'][fi+offset].copy()
        elif method in ('rtc','training_time_rtc'):prefix=table['action'][fi:min(end,fi+h)].copy()
        began=time.perf_counter()
        predicted=policy.predict(obs,method,prefix,delay if prefix is not None else 0)
        start=delay if prefix is not None else 0
        smoother.reset()
        if correction is None:
            predicted = smoother.chunk(predicted, start)
        latencies.append((time.perf_counter()-began)*1000)
        for k in range(start,min(h,end-fi-offset)):
            target_frame=fi+offset+k;base=predicted[k]
            if correction:
                latest=observation(ds[lookup[ei,target_frame]])
                base=correction.correct(latest,base,k,delay)
                base=smoother.action(base)
            total+=float(np.abs(base-table['action'][target_frame]).sum());count+=len(base)
    result={'protocol':'offline held-out observations; demonstration prefixes/future states simulate delay; NOT a closed-loop success-rate test',
        'method':method,'correction':bool(correction),'delay_steps':args.delay_steps,'source_episode_indices':ids,
        'action_smoothing':smoothing,
        'smoothing_application':'corrected_commands_causal' if correction else 'predicted_plan',
        'mean_absolute_error_rad':total/count if count else None,'compared_action_coordinates':count,
        'inference_mean_ms':float(np.mean(latencies)),'inference_p95_ms':float(np.percentile(latencies,95)),
        'base_fingerprint':policy.fingerprint}
    if not count:raise ValueError('No evaluable action suffix after the requested delay')
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as stream:json.dump(result,stream,indent=2)
    print(json.dumps(result),flush=True)
    return result


def train_correction(args):
    cache=Path(args.cache);meta=json.loads((cache/'cache.json').read_text())
    if meta['format']!='so101_a2c2_cache_v1':raise ValueError('A2C2 cache required')
    with np.load(cache/'samples.npz',allow_pickle=False) as data:arrays={k:data[k].copy() for k in data.files}
    mask=np.isin(arrays['episode'],meta['train_episodes'])
    val=np.isin(arrays['episode'],meta['validation_episodes'])
    if not mask.any():raise ValueError('No correction training samples in base training split')
    torch.manual_seed(args.seed);rng=np.random.default_rng(args.seed)
    model=CorrectionHead(meta['features'],meta['joints'],args.hidden).to(args.device)
    optimizer=torch.optim.AdamW(model.parameters(),lr=1e-4)
    tensors={k:torch.from_numpy(v).to(args.device) for k,v in arrays.items() if k!='episode'}
    indices=np.flatnonzero(mask)
    for step in range(1,args.steps+1):
        chosen=rng.choice(indices,min(args.batch_size,len(indices)),replace=False)
        batch={k:v[chosen] for k,v in tensors.items()}
        prediction=model(batch['features'],batch['base'],batch['position'],batch['delay'])
        loss=(prediction-batch['target']).square().mean()
        if not torch.isfinite(loss):raise ValueError('Nonfinite correction loss')
        optimizer.zero_grad(set_to_none=True);loss.backward();torch.nn.utils.clip_grad_norm_(model.parameters(),1.);optimizer.step()
    metrics={'step':args.steps,'residual_mse':float(loss.detach()),'validation_mse':None}
    if val.any():
        model.eval()
        with torch.no_grad():
            b={k:v[np.flatnonzero(val)] for k,v in tensors.items()}
            metrics['validation_mse']=float((model(b['features'],b['base'],b['position'],b['delay'])-b['target']).square().mean())
    output=Path(args.output);output.mkdir(parents=True,exist_ok=False)
    save_file({k:v.detach().cpu().contiguous() for k,v in model.state_dict().items()},str(output/'model.safetensors'))
    (output/'config.json').write_text(json.dumps({**meta,'format':'so101_a2c2_v1','hidden':args.hidden},indent=2))
    (output/'metrics.json').write_text(json.dumps(metrics,indent=2))
    print(json.dumps(metrics),flush=True)
    return output

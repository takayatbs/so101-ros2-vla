"""Compact image-conditioned rectified-flow policy and published RTC mechanisms.

This is a trainable SO101 backbone, not downloaded pi0/SmolVLA weights.
RTC equations 2-5: arxiv.org/abs/2506.07339.
Clean-prefix training/inference: arxiv.org/abs/2512.05964.
"""
import json
import math
import time
from pathlib import Path
import numpy as np
import torch
from torch import nn

FORMAT = 'so101_flow_v1'


class FlowModel(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        d,h,a = config['dim_model'],config['chunk_size'],len(config['joint_names'])
        self.cnn = nn.Sequential(nn.Conv2d(3,16,5,2,2),nn.SiLU(),nn.Conv2d(16,32,3,2,1),nn.SiLU(),
                                 nn.Conv2d(32,d,3,2,1),nn.SiLU(),nn.AdaptiveAvgPool2d(1),nn.Flatten())
        self.state = nn.Linear(a,d)
        self.context = nn.Sequential(nn.Linear(d*(len(config['cameras'])+1),d),nn.SiLU(),nn.Linear(d,d))
        self.action = nn.Linear(a,d)
        self.time = nn.Sequential(nn.Linear(4,d),nn.SiLU(),nn.Linear(d,d))
        self.position = nn.Parameter(torch.randn(1,h,d)*.02)
        layer = nn.TransformerEncoderLayer(d,config['n_heads'],d*4,config.get('dropout',0.),
                                          batch_first=True,norm_first=True,activation='gelu')
        self.blocks = nn.TransformerEncoder(layer,config['n_layers'],enable_nested_tensor=False)
        self.out = nn.Linear(d,a)

    def encode(self, state, images):
        return self.context(torch.cat([self.state(state),*(self.cnn(images[n]) for n in self.config['cameras'])],-1))

    def velocity(self, x, times, context, padding=None):
        t = times[...,None]
        embedded = self.time(torch.cat([t,t*t,torch.sin(math.pi*t),torch.cos(math.pi*t)],-1))
        tokens = self.action(x)+embedded+self.position+context[:,None,:]
        return self.out(self.blocks(tokens,src_key_padding_mask=padding))


def prefix_weights(length, known, overlap, device=None):
    known,overlap = min(known,overlap),min(length,overlap)
    weights = torch.zeros(length,device=device)
    weights[:known] = 1
    if overlap > known:
        c = torch.arange(overlap-known,0,-1,device=device)/(overlap-known+1)
        weights[known:overlap] = c*torch.expm1(c)/(math.e-1)
    return weights


def flow_loss(model, state, images, actions, padding, delays=None, noise=None, times=None):
    b,h,_ = actions.shape
    noise = torch.randn_like(actions) if noise is None else noise
    times = torch.rand(b,device=actions.device) if times is None else times
    local_time = times[:,None].expand(b,h).clone()
    prefix = torch.zeros_like(padding)
    if delays is not None:
        # Always leave at least one non-padding postfix token to learn.
        delays = torch.minimum(delays,(~padding).sum(1)-1).clamp_min(0)
        prefix = torch.arange(h,device=actions.device)[None,:] < delays[:,None]
        local_time = torch.where(prefix,1.,local_time)
    x = local_time[...,None]*actions+(1-local_time[...,None])*noise
    prediction = model.velocity(x,local_time,model.encode(state,images),padding)
    valid = (~padding & ~prefix)[...,None]
    return ((prediction-(actions-noise)).square()*valid).sum()/(valid.sum()*actions.shape[-1]).clamp_min(1)


def sample(model, context, steps, method='standard', previous=None, delay=0, beta=5., noise=None):
    config = model.config
    shape = (len(context),config['chunk_size'],len(config['joint_names']))
    x = torch.randn(shape,device=context.device) if noise is None else noise.clone()
    previous_length = 0 if previous is None else previous.shape[1]
    if delay > previous_length:
        raise ValueError('Committed prefix is shorter than inference delay')
    weights = prefix_weights(shape[1],delay,previous_length,x.device)[None,:,None]
    target = torch.zeros_like(x)
    if previous is not None:target[:,:previous_length] = previous
    prefix = torch.arange(shape[1],device=x.device)[None,:,None] < delay
    for step in range(steps):
        tau = step/steps
        local_time = torch.full(shape[:2],tau,device=x.device)
        if method == 'training_time_rtc' and previous is not None:
            x = torch.where(prefix,target,x)
            local_time = torch.where(prefix[...,0],1.,local_time)
        if method == 'rtc' and previous is not None:
            with torch.enable_grad():
                x = x.detach().requires_grad_(True)
                v = model.velocity(x,local_time,context)
                endpoint = x+(1-tau)*v
                error = ((target-endpoint)*weights).detach()
                correction = torch.autograd.grad(endpoint,x,grad_outputs=error)[0]
                r2 = (1-tau)**2/(tau*tau+(1-tau)**2)
                strength = min(beta,(1-tau)/(max(tau,1e-8)*max(r2,1e-8)))
                v = v+strength*correction
        else:
            with torch.no_grad():v = model.velocity(x,local_time,context)
        x = (x+v/steps).detach()
    if method == 'training_time_rtc':x = torch.where(prefix,target,x)
    return x


class FlowPolicy:
    def __init__(self, checkpoint, device='cpu'):
        from safetensors.torch import load_file
        from .converter import sha256_file
        self.folder,self.device = Path(checkpoint),device
        self.config = json.loads((self.folder/'config.json').read_text())
        if self.config['format'] != FORMAT:raise ValueError('Flow checkpoint required')
        self.model = FlowModel(self.config).to(device).eval()
        self.model.load_state_dict(load_file(str(self.folder/'model.safetensors')),strict=True)
        for p in self.model.parameters():p.requires_grad_(False)
        self.statistics = json.loads((self.folder/'normalization.json').read_text())
        self.stats = {k:torch.tensor(v,dtype=torch.float32,device=device) for k,v in self.statistics.items()}
        for key,value in self.stats.items():
            if value.shape != (len(self.config['joint_names']),) or not torch.isfinite(value).all() or (key.endswith('_std') and (value<=0).any()):
                raise ValueError('Invalid flow normalization statistics')
        self.fingerprint = sha256_file(self.folder/'model.safetensors')+':'+sha256_file(self.folder/'config.json')+':'+sha256_file(self.folder/'normalization.json')

    def reset(self):pass

    def features(self, observation):
        state = torch.as_tensor(np.asarray(observation['observation.state']).copy(),dtype=torch.float32,device=self.device)[None]
        if not torch.isfinite(state).all():raise ValueError('Nonfinite observation')
        state = (state-self.stats['state_mean'])/self.stats['state_std']
        images = {n:torch.as_tensor(np.asarray(observation['observation.images.'+n]).copy(),device=self.device).permute(2,0,1)[None].float()/255 for n in self.config['cameras']}
        with torch.no_grad():return self.model.encode(state,images)

    def predict(self, observation, method='standard', previous=None, delay=0):
        context = self.features(observation)
        prefix = None
        if previous is not None:
            prefix = (torch.as_tensor(previous,dtype=torch.float32,device=self.device)[None]-self.stats['action_mean'])/self.stats['action_std']
        result = sample(self.model,context,self.config['denoise_steps'],method,prefix,delay,
                        self.config.get('max_guidance_weight',5.))
        result = result*self.stats['action_std']+self.stats['action_mean']
        result = result[0].cpu().numpy()
        if not np.isfinite(result).all():raise ValueError('Nonfinite flow action')
        return result


class CorrectionHead(nn.Module):
    def __init__(self, features, joints, hidden=128):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(features+joints+4,hidden),nn.LayerNorm(hidden),nn.SiLU(),
                                 nn.Linear(hidden,hidden),nn.SiLU(),nn.Linear(hidden,joints))

    def forward(self, features, base, position, delay):
        k = position[:,None]
        clock = torch.cat([k,torch.sin(math.pi*k),torch.cos(math.pi*k),delay[:,None]],-1)
        return self.net(torch.cat([features,base,clock],-1))


class A2C2:
    def __init__(self, checkpoint, policy):
        from safetensors.torch import load_file
        folder = Path(checkpoint)
        self.config = json.loads((folder/'config.json').read_text())
        if self.config.get('format') != 'so101_a2c2_v1':raise ValueError('A trained A2C2 checkpoint is required')
        if self.config.get('base_fingerprint') != policy.fingerprint:
            raise ValueError('A2C2 was trained for a different base checkpoint/normalization')
        self.policy = policy
        self.latency_ms = 0.
        self.model = CorrectionHead(policy.config['dim_model'],len(policy.config['joint_names']),self.config['hidden']).to(policy.device).eval()
        self.model.load_state_dict(load_file(str(folder/'model.safetensors')),strict=True)

    def correct(self, observation, base, position, delay):
        began=time.perf_counter()
        p = self.policy;h = p.config['chunk_size']
        features = p.features(observation)
        action = torch.tensor(np.asarray(base).copy(),device=p.device,dtype=torch.float32)[None]
        normal = (action-p.stats['action_mean'])/p.stats['action_std']
        with torch.no_grad():
            residual = self.model(features,normal,torch.tensor([position/h],device=p.device),
                                  torch.tensor([delay/h],device=p.device))*p.stats['action_std']
        result = (action+residual)[0].cpu().numpy()
        self.latency_ms=(time.perf_counter()-began)*1000
        if not np.isfinite(result).all():raise ValueError('Nonfinite A2C2 action')
        return result

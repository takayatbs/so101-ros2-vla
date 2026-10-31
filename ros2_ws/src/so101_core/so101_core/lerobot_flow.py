# Copyright 2025 HuggingFace Inc. team. All rights reserved.
# Copyright 2025 Physical Intelligence and The HuggingFace Inc. team. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# LeRobot reference: https://github.com/huggingface/lerobot
# Revision: 22bd7a2f489b367d8df42de803b1e8c4ca63a3f9
# Upstream: src/lerobot/policies/smolvla/modeling_smolvla.py
# Upstream: src/lerobot/policies/pi0/modeling_pi0.py
# Upstream: src/lerobot/policies/pi05/modeling_pi05.py
# Project modifications: Per-action time conditioning, clean-prefix training, shared prefix caching and RTC guidance.
# License and upstream third-party notices: LICENSES/LeRobot-LICENSE.txt

"""Continuous LeRobot backbones with clean-prefix and per-action flow times.

Uses the LeRobot SmolVLA / PI0 / PI05 embedding and attention contracts.
Flow convention: t=1 is noise, t=0 is action. No pretrained weights are replaced.
"""
import importlib
import types

import torch
from torch.nn import functional as F

from .flow import prefix_weights


def _token_adarms(self, x, cond=None):
    # LeRobot's PiGemmaRMSNorm inserts a sequence dimension unconditionally.
    # Per-token time conditioning already has that dimension.
    if cond is None or cond.ndim != 3:
        return self._so101_original_forward(x, cond)
    dtype = x.dtype
    normed = x.float() * torch.rsqrt(x.float().square().mean(-1, keepdim=True) + self.eps)
    scale, shift, gate = self.dense(cond).chunk(3, dim=-1)
    return (normed * (1 + scale.float()) + shift.float()).to(dtype), gate.to(dtype)


class LeRobotFlow:
    def __init__(self, policy, family):
        self.policy, self.family, self.model = policy, family, policy.model
        self.api = importlib.import_module(f'lerobot.policies.{family}.modeling_{family}')
        self.is_smol = family == 'smolvla'
        self.backbone = self.model.vlm_with_expert if self.is_smol else self.model.paligemma_with_expert
        if family == 'pi05':
            for module in self.model.modules():
                if type(module).__name__ == 'PiGemmaRMSNorm' and module.dense is not None:
                    if not hasattr(module, '_so101_original_forward'):
                        module._so101_original_forward = module.forward
                        module.forward = types.MethodType(_token_adarms, module)

    def encode(self, batch, cache=False):
        p, m = self.policy, self.model
        images, masks = p.prepare_images(batch) if self.is_smol else p._preprocess_images(batch)
        args = (images, masks, batch['observation.language.tokens'], batch['observation.language.attention_mask'])
        state = p.prepare_state(batch) if self.family != 'pi05' else None
        prefix, pad, att = m.embed_prefix(*args, state=state) if self.is_smol else m.embed_prefix(*args)
        if not self.is_smol:
            dtype = self.backbone.paligemma.model.language_model.layers[0].self_attn.q_proj.weight.dtype
            prefix = prefix.to(dtype)
        result = {'prefix': prefix, 'pad': pad, 'att': att, 'state': state, 'cache': None}
        if cache:
            mask = self.api.make_att_2d_masks(pad, att)
            kw = {} if self.is_smol else {'adarms_cond': [None, None]}
            _, result['cache'] = self.backbone.forward(
                attention_mask=mask if self.is_smol else self.api.prepare_attention_masks_4d(mask),
                position_ids=pad.long().cumsum(1)-1, inputs_embeds=[prefix, None],
                past_key_values=None, use_cache=True, **kw)
        return result

    def suffix(self, x, times, state):
        m = self.model
        b, h = x.shape[:2]
        # Flatten only for the sinusoidal helper, which accepts a vector.
        time_emb = self.api.create_sinusoidal_pos_embedding(
            times.reshape(-1), m.action_in_proj.out_features,
            min_period=m.config.min_period, max_period=m.config.max_period,
            device=x.device).reshape(b, h, -1).to(x.dtype)
        action = m.action_in_proj(x)
        cond = None
        if self.family == 'pi05':
            cond = F.silu(m.time_mlp_out(F.silu(m.time_mlp_in(time_emb))))
            tokens = action
        else:
            tokens = m.action_time_mlp_out(F.silu(m.action_time_mlp_in(torch.cat([action, time_emb], -1))))
        if self.family == 'pi0':
            tokens = torch.cat([m.state_proj(state.to(m.state_proj.weight.dtype))[:, None], tokens], 1)
            att = [1, 1] + [0] * (h-1)
        elif self.is_smol:
            att = [1] * h
        else:
            att = [1] + [0] * (h-1)
        pad = torch.ones(tokens.shape[:2], device=x.device, dtype=torch.bool)
        att = torch.tensor(att, device=x.device, dtype=torch.bool)[None].expand(b, -1)
        return tokens, pad, att, cond

    def velocity(self, x, times, encoded):
        tokens, pad, att, cond = self.suffix(x, times, encoded['state'])
        prefix_pad = encoded['pad']
        cached = encoded['cache']
        tokens = tokens.to(encoded['prefix'].dtype)
        if cached is None:
            all_pad = torch.cat([prefix_pad, pad], 1)
            all_att = torch.cat([encoded['att'], att], 1)
            mask = self.api.make_att_2d_masks(all_pad, all_att)
            positions = all_pad.long().cumsum(1)-1
            inputs = [encoded['prefix'], tokens]
        else:
            mask = torch.cat([prefix_pad[:, None].expand(-1, len(pad[0]), -1),
                              self.api.make_att_2d_masks(pad, att)], -1)
            positions = prefix_pad.sum(-1)[:, None] + pad.long().cumsum(1)-1
            inputs = [None, tokens]
            if not self.is_smol:
                cached = self.api.clone_past_key_values(cached)
        kw = {} if self.is_smol else {'adarms_cond': [None, cond]}
        outputs, _ = self.backbone.forward(
            attention_mask=mask if self.is_smol else self.api.prepare_attention_masks_4d(mask),
            position_ids=positions, inputs_embeds=inputs, past_key_values=cached,
            use_cache=self.is_smol and cached is not None, **kw)
        if self.is_smol and cached is not None:
            cached.crop(prefix_pad.shape[1])
        return self.model.action_out_proj(outputs[1][:, -x.shape[1]:].float())

    def loss(self, batch, max_delay, clean_prefix=False):
        actions = self.policy.prepare_action(batch)
        noise = self.model.sample_noise(actions.shape, actions.device)
        b, h, a = actions.shape
        times = self.model.sample_time(b, actions.device)[:, None].expand(b, h).clone()
        padding = batch['action_is_pad']
        prefix = torch.zeros_like(padding)
        if clean_prefix:
            delay = torch.randint(max_delay+1, (b,), device=actions.device)
            delay = torch.minimum(delay, (~padding).sum(1)-1).clamp_min(0)
            prefix = torch.arange(h, device=actions.device)[None] < delay[:, None]
            times = torch.where(prefix, 0., times)
        noisy = times[..., None]*noise + (1-times[..., None])*actions
        velocity = self.velocity(noisy, times, self.encode(batch))
        a = self.policy.config.action_feature.shape[0]
        valid = (~padding & ~prefix)[..., None]
        return (((velocity-(noise-actions))[..., :a].square())*valid).sum()/(valid.sum()*a).clamp_min(1)

    def sample(self, batch, steps, method, previous=None, delay=0):
        m = self.model
        with torch.no_grad():
            encoded = self.encode(batch, cache=True)
        b = len(encoded['pad']); h = m.config.chunk_size
        x = m.sample_noise((b, h, m.config.max_action_dim), encoded['pad'].device)
        target = torch.zeros_like(x)
        n = 0 if previous is None else min(previous.shape[1], h)
        if delay > n:
            raise ValueError('Committed prefix is shorter than inference delay')
        if n:
            target[:, :n, :previous.shape[-1]] = previous[:, :n]
        prefix = torch.arange(h, device=x.device)[None] < delay
        weights = prefix_weights(h, delay, n, x.device)[None, :, None]
        # Guidance applies to real joints, never to padded model dimensions.
        dimensions = torch.arange(x.shape[-1], device=x.device) < self.policy.config.action_feature.shape[0]
        for step in range(steps):
            t = 1-step/steps
            times = torch.full((b, h), t, device=x.device)
            if method == 'training_time_rtc' and n:
                x = torch.where(prefix[..., None], target, x)
                times = torch.where(prefix, 0., times)
            if method == 'rtc' and n:
                with torch.enable_grad():
                    x = x.detach().requires_grad_(True)
                    v = self.velocity(x, times, encoded)
                    endpoint = x-t*v
                    error = ((target-endpoint)*weights*dimensions).detach()
                    gradient = torch.autograd.grad(endpoint, x, error)[0]
                    tau = 1-t
                    r2 = t*t/(tau*tau+t*t)
                    strength = min(5., t/(max(tau, 1e-8)*max(r2, 1e-8)))
                    v = v-strength*gradient
            else:
                with torch.no_grad():
                    v = self.velocity(x, times, encoded)
            x = (x-v/steps).detach()
        if method == 'training_time_rtc' and n:
            x = torch.where(prefix[..., None], target, x)
        return x[..., :getattr(self, 'return_action_dim', self.policy.config.action_feature.shape[0])]

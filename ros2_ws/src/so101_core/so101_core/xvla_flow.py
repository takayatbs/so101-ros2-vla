# Copyright 2025 The HuggingFace Inc. team and 2toINF (https://github.com/2toINF)
# Copyright 2025 2toINF (https://github.com/2toINF)
# SPDX-License-Identifier: Apache-2.0
#
# LeRobot reference: https://github.com/huggingface/lerobot
# Revision: 22bd7a2f489b367d8df42de803b1e8c4ca63a3f9
# Upstream: src/lerobot/policies/xvla/modeling_xvla.py
# Upstream: src/lerobot/policies/xvla/soft_transformer.py
# Project modifications: Per-action time embeddings, clean-prefix training and RTC endpoint guidance.
# License and upstream third-party notices: LICENSES/LeRobot-LICENSE.txt

"""XVLA endpoint prediction with per-action times and RTC endpoint guidance."""
import torch

from .flow import prefix_weights


def transformer_tokens(model, inputs, noisy, times):
    from lerobot.policies.xvla.soft_transformer import timestep_embedding
    head = model.transformer
    b, h = noisy.shape[:2]
    proprio, noisy = model.action_space.preprocess(inputs['proprio'], noisy)
    time = timestep_embedding(times.flatten(), head.dim_time).reshape(b, h, -1)
    proprio = proprio[:, None].expand(-1, h, -1)
    domain = inputs['domain_id']
    x = head.action_encoder(torch.cat([noisy, proprio, time.to(noisy.dtype)], -1), domain)
    vlm, aux = inputs['vlm_features'], inputs['aux_visual_inputs']
    if head.use_hetero_proj:
        x = torch.cat([x, head.vlm_proj(vlm, domain), head.aux_visual_proj(aux, domain)], 1)
    else:
        x = torch.cat([x, head.vlm_proj(vlm), head.aux_visual_proj(aux)], 1)
    if x.shape[1] > head.pos_emb.shape[1]:
        raise ValueError('XVLA token sequence exceeds positional embeddings')
    x = x+head.pos_emb[:, :x.shape[1]]
    if head.len_soft_prompts:
        x = torch.cat([x, head.soft_prompt_hub(domain).view(b, head.len_soft_prompts, head.hidden_size)], 1)
    for block in head.blocks:
        x = block(x)
    return head.action_decoder(head.norm(x[:, :h]), domain)


class XVLAFlow:
    def __init__(self, policy):
        self.policy, self.model = policy, policy.model

    def encode(self, batch):
        values = self.policy._build_model_inputs(batch)
        dtype = self.model._get_target_dtype()
        values['image_input'] = values['image_input'].to(dtype)
        values['proprio'] = values['proprio'].to(dtype)
        enc = self.model.forward_vlm(values['input_ids'], values['image_input'], values['image_mask'])
        return {**enc, 'proprio': values['proprio'], 'domain_id': values['domain_id']}

    def loss(self, batch, max_delay, clean_prefix=False):
        actions = self.policy._prepare_action_targets(batch).to(self.model._get_target_dtype())
        b, h, a = actions.shape
        times = torch.rand(b, device=actions.device)[:, None].expand(b, h).clone()
        padding = batch['action_is_pad']
        prefix = torch.zeros_like(padding)
        if clean_prefix:
            delays = torch.randint(max_delay+1, (b,), device=actions.device)
            delays = torch.minimum(delays, (~padding).sum(1)-1).clamp_min(0)
            prefix = torch.arange(h, device=actions.device)[None] < delays[:, None]
            times = torch.where(prefix, 0., times)
        noisy = times[..., None]*torch.randn_like(actions)+(1-times[..., None])*actions
        endpoint = transformer_tokens(self.model, self.encode(batch), noisy, times)
        a = self.policy.config.action_feature.shape[0]
        valid = (~padding & ~prefix)[..., None]
        return ((endpoint[..., :a].float()-actions[..., :a].float()).square()*valid).sum()/(valid.sum()*a).clamp_min(1)

    def sample(self, batch, steps, method, previous=None, delay=0):
        with torch.no_grad():
            inputs = self.encode(batch)
        b = len(inputs['proprio']); h = self.model.chunk_size; a = self.model.dim_action
        noise = torch.randn(b, h, a, device=inputs['proprio'].device, dtype=inputs['proprio'].dtype)
        endpoint = torch.zeros_like(noise)
        target = torch.zeros_like(noise)
        n = 0 if previous is None else min(previous.shape[1], h)
        if delay > n:
            raise ValueError('Committed prefix is shorter than inference delay')
        if n:
            target[:, :n, :previous.shape[-1]] = previous[:, :n]
        prefix = torch.arange(h, device=noise.device)[None] < delay
        weights = prefix_weights(h, delay, n, noise.device)[None, :, None]
        dimensions = torch.arange(a, device=noise.device) < self.policy.config.action_feature.shape[0]
        for i in range(steps, 0, -1):
            t = i/steps
            times = torch.full((b, h), t, device=noise.device)
            x = noise*t+endpoint*(1-t)
            if method == 'training_time_rtc' and n:
                x = torch.where(prefix[..., None], target, x)
                times = torch.where(prefix, 0., times)
            if method == 'rtc' and n:
                with torch.enable_grad():
                    x = x.detach().requires_grad_(True)
                    endpoint = transformer_tokens(self.model, inputs, x, times)
                    error = ((target-endpoint)*weights*dimensions).detach()
                    gradient = torch.autograd.grad(endpoint, x, error)[0]
                    tau = 1-t; r2 = t*t/(tau*tau+t*t)
                    strength = min(5., t/(max(tau, 1e-8)*max(r2, 1e-8)))
                    endpoint = endpoint+t*strength*gradient
            else:
                with torch.no_grad():
                    endpoint = transformer_tokens(self.model, inputs, x, times)
            endpoint = endpoint.detach()
            if method == 'training_time_rtc' and n:
                endpoint = torch.where(prefix[..., None], target, endpoint)
        return self.model.action_space.postprocess(endpoint)

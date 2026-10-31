# Copyright 2024 NVIDIA Corporation and The HuggingFace Inc. team. All rights reserved.
# Copyright 2025 NVIDIA Corporation and The HuggingFace Inc. team. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# LeRobot reference: https://github.com/huggingface/lerobot
# Revision: 22bd7a2f489b367d8df42de803b1e8c4ca63a3f9
# Upstream: src/lerobot/policies/groot/modeling_groot.py
# Upstream: src/lerobot/policies/groot/action_head/cross_attention_dit.py
# Project modifications: Per-action time conditioning and gradient-guided RTC; integration with the SO-101 scheduling contract.
# License and upstream third-party notices: LICENSES/LeRobot-LICENSE.txt

"""GR00T N1.7 flow velocity with per-token time conditioning.

Keeps the pretrained action encoder/DiT/decoder parameters. This implements
gradient-guided RTC, independently of GR00T's native overlap blend.
"""
from types import MethodType, SimpleNamespace

import torch
from torch.nn import functional as F

from .lerobot_flow import LeRobotFlow


def _token_norm(self, x, temb=None):
    if temb is None or temb.ndim != 3:
        return self._so101_original_forward(x, temb)
    b, h, d = x.shape
    return self._so101_original_forward(x.reshape(b*h, 1, d), temb.reshape(b*h, -1)).reshape(b, h, d)


def action_tokens(head, x, times, embodiment):
    encoder = head.action_encoder
    time = encoder.pos_encoding(times).to(x.dtype)
    action = encoder.W1(x, embodiment)
    value = encoder.W2(torch.cat([action, time], -1), embodiment)
    return encoder.W3(F.silu(value), embodiment)


def dit_tokens(dit, hidden, backbone, times):
    b, h = times.shape
    temb = dit.timestep_encoder(times.flatten()).reshape(b, h, -1)
    vl = backbone.backbone_features.contiguous()
    hidden = hidden.contiguous()
    alternate = hasattr(dit, 'attend_text_every_n_blocks')
    for index, block in enumerate(dit.transformer_blocks):
        self_attention = index % 2 == 1 and dit.config.interleave_self_attention
        mask = None
        if alternate and not self_attention:
            mask = ((~backbone.image_mask) if index % (2*dit.attend_text_every_n_blocks) == 0
                    else backbone.image_mask) & backbone.backbone_attention_mask
        hidden = block(hidden, attention_mask=None,
            encoder_hidden_states=None if self_attention else vl,
            encoder_attention_mask=mask, temb=temb)
    shift, scale = dit.proj_out_1(F.silu(temb)).chunk(2, dim=-1)
    return dit.proj_out_2(dit.norm_out(hidden)*(1+scale)+shift)


class GrootFlow(LeRobotFlow):
    def __init__(self, policy):
        self.policy, self.family = policy, 'groot'
        self.network = policy._groot_model
        self.head = self.network.action_head
        head = self.head
        for module in head.model.modules():
            if type(module).__name__ == 'AdaLayerNorm' and not hasattr(module, '_so101_original_forward'):
                module._so101_original_forward = module.forward
                module.forward = MethodType(_token_norm, module)
        self.model = SimpleNamespace(
            config=SimpleNamespace(chunk_size=head.action_horizon, max_action_dim=head.action_dim),
            sample_noise=lambda shape, device: torch.randn(shape, device=device),
        )
        self.return_action_dim = head.action_dim

    def _autocast(self):
        return torch.autocast(device_type=next(self.network.parameters()).device.type,
                              dtype=torch.bfloat16, enabled=self.policy.config.use_bf16)

    def encode(self, batch, cache=False):
        values = self.policy._filter_groot_inputs(batch, include_action=not cache)
        self.head.set_frozen_modules_to_eval_mode()
        with self._autocast():
            backbone_input, action_input = self.network.prepare_input(values)
            backbone = self.head.process_backbone_output(self.network.backbone(backbone_input))
            state = action_input.state.reshape(action_input.state.shape[0], 1, -1)
            state = self.head.state_encoder(state, action_input.embodiment_id)
            if self.head.training and not cache and self.head.state_dropout_prob > 0:
                drop = torch.rand(len(state), device=state.device) < self.head.state_dropout_prob
                state = state*(~drop)[:, None, None].to(state.dtype)
        return {'pad': torch.ones(state.shape[:2], device=state.device, dtype=torch.bool),
                'backbone': backbone, 'state': state, 'action_input': action_input}

    def velocity(self, x, times, encoded):
        # N1.7 uses t=0 at noise and t=1 at clean action.
        buckets = ((1-times)*self.head.num_timestep_buckets).long()
        inputs = encoded['action_input']
        with self._autocast():
            tokens = action_tokens(self.head, x, buckets, inputs.embodiment_id)
            if self.head.config.add_pos_embed:
                pos = torch.arange(x.shape[1], device=x.device)
                tokens = tokens+self.head.position_embedding(pos)[None]
            state = encoded['state']
            hidden = torch.cat([state, tokens], 1)
            # State tokens keep the common denoising time; clean action prefix
            # tokens alone receive their execution-time endpoint embedding.
            state_time = buckets.min(1).values[:, None].expand(-1, state.shape[1])
            all_times = torch.cat([state_time, buckets], 1)
            prediction = dit_tokens(self.head.model, hidden, encoded['backbone'], all_times)
            output = self.head.action_decoder(prediction, inputs.embodiment_id)[:, -x.shape[1]:]
        return -output.float()

    def loss(self, batch, max_delay, clean_prefix=False):
        encoded = self.encode(batch)
        inputs = encoded['action_input']
        actions = inputs.action
        b, h, a = actions.shape
        noise = torch.randn_like(actions)
        times = (1-self.head.sample_time(b, actions.device, actions.dtype))[:, None].expand(b, h).clone()
        mask = inputs.action_mask.bool()
        padding = batch.get('action_is_pad')
        if padding is not None:
            padding = F.pad(padding, (0, h-padding.shape[1]), value=True)
            mask = mask & ~padding[..., None]
        if clean_prefix:
            delays = torch.randint(max_delay+1, (b,), device=actions.device)
            delays = torch.minimum(delays, mask.any(-1).sum(1)-1).clamp_min(0)
            prefix = torch.arange(h, device=actions.device)[None] < delays[:, None]
            times = torch.where(prefix, 0., times)
            mask = mask & ~prefix[..., None]
        noisy = times[..., None]*noise+(1-times[..., None])*actions
        prediction = self.velocity(noisy, times, encoded)
        return ((prediction-(noise-actions)).square()*mask).sum()/mask.sum().clamp_min(1)

# Copyright 2024 Columbia Artificial Intelligence, Robotics Lab,
# and The HuggingFace Inc. team. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# LeRobot reference: https://github.com/huggingface/lerobot
# Revision: 22bd7a2f489b367d8df42de803b1e8c4ca63a3f9
# Upstream: src/lerobot/policies/diffusion/modeling_diffusion.py
# Project modifications: Differentiable prefix guidance through the denoiser and the SO-101 action-chunk interface.
# License and upstream third-party notices: LICENSES/LeRobot-LICENSE.txt

"""Differentiable clean-action prefix guidance for DDPM/DDIM policies."""
import torch

from .flow import prefix_weights


def predict_diffusion_rtc(policy, batch, previous=None, delay=0):
    diffusion, cfg = policy.diffusion, policy.config
    batch = dict(batch)
    images = []
    for name in cfg.image_features:
        value = batch[name]
        images.append(value[:, None] if value.ndim == 4 else value)
    if images:
        batch['observation.images'] = torch.stack(images, dim=-4)
    with torch.no_grad():
        context = diffusion._prepare_global_conditioning(batch)
    x = torch.randn(len(context), cfg.horizon, cfg.action_feature.shape[0], device=context.device)
    n = 0 if previous is None else min(previous.shape[1], cfg.horizon)
    if delay > n:
        raise ValueError('Committed prefix is shorter than inference delay')
    target = torch.zeros_like(x)
    if n:
        target[:, :n] = previous[:, :n]
    weights = prefix_weights(cfg.horizon, delay, n, x.device)[None, :, None]
    scheduler = diffusion.noise_scheduler
    scheduler.set_timesteps(diffusion.num_inference_steps)
    for t in scheduler.timesteps:
        if n:
            with torch.enable_grad():
                x = x.detach().requires_grad_(True)
                prediction = diffusion.unet(x, torch.full(x.shape[:1], t, device=x.device), global_cond=context)
                alpha = scheduler.alphas_cumprod[t].to(x.device)
                if scheduler.config.prediction_type == 'epsilon':
                    endpoint = (x-(1-alpha).sqrt()*prediction)/alpha.sqrt()
                elif scheduler.config.prediction_type == 'sample':
                    endpoint = prediction
                else:
                    raise ValueError('RTC requires diffusion prediction_type epsilon or sample')
                error = ((target-endpoint)*weights).detach()
                gradient = torch.autograd.grad(endpoint, x, error)[0]
                # Effective noise/action interpolation coordinate for VP diffusion.
                tau = float(alpha.sqrt()/(alpha.sqrt()+(1-alpha).sqrt()))
                r2 = (1-tau)**2/(tau*tau+(1-tau)**2)
                strength = min(5., (1-tau)/(max(tau, 1e-8)*max(r2, 1e-8)))
                correction = (1-tau)*strength*gradient
                if scheduler.config.prediction_type == 'epsilon':
                    prediction = prediction-correction*(alpha/(1-alpha).clamp_min(1e-8)).sqrt()
                else:
                    prediction = prediction+correction
        else:
            with torch.no_grad():
                prediction = diffusion.unet(x, torch.full(x.shape[:1], t, device=x.device), global_cond=context)
        with torch.no_grad():
            x = scheduler.step(prediction.detach(), t, x.detach()).prev_sample
    return x[:, :cfg.n_action_steps]

"""Exercise real transformer code at tiny dimensions; no downloaded weights."""
import importlib
from types import SimpleNamespace

import pytest

torch = pytest.importorskip('torch')
pytest.importorskip('lerobot.policies.factory')

from so101_core.lerobot_policy import feature_schema
from so101_core.lerobot_flow import LeRobotFlow


@pytest.fixture(params=['smolvla', 'pi0', 'pi05'])
def tiny_flow(request, monkeypatch):
    torch.set_num_threads(2)
    family = request.param
    api = importlib.import_module(f'lerobot.policies.{family}.modeling_{family}')
    config_api = importlib.import_module(f'lerobot.policies.{family}.configuration_{family}')
    if family == 'smolvla':
        from transformers import SmolVLMConfig
        assets = importlib.import_module('lerobot.policies.smolvla.smolvlm_with_expert')
        small = SmolVLMConfig(
            vision_config={'hidden_size': 32, 'intermediate_size': 64, 'num_hidden_layers': 1,
                           'num_attention_heads': 4, 'image_size': 32, 'patch_size': 8},
            text_config={'model_type': 'smollm3', 'hidden_size': 32, 'intermediate_size': 64,
                         'num_hidden_layers': 2, 'num_attention_heads': 4, 'num_key_value_heads': 2,
                         'head_dim': 8, 'vocab_size': 1000}, image_token_id=990, scale_factor=2)
        # SmolVLM uses LlamaConfig in the supported checkpoints.
        from transformers import LlamaConfig
        small.text_config = LlamaConfig(hidden_size=32, intermediate_size=64, num_hidden_layers=2,
            num_attention_heads=4, num_key_value_heads=2, head_dim=8, vocab_size=1000)
        monkeypatch.setattr(assets.AutoConfig, 'from_pretrained', lambda *a, **kw: small)
        monkeypatch.setattr(assets.AutoProcessor, 'from_pretrained', lambda *a, **kw:
                            SimpleNamespace(tokenizer=SimpleNamespace(fake_image_token_id=0, global_image_token_id=1)))
        cfg = config_api.SmolVLAConfig(device='cpu', chunk_size=6, n_action_steps=6, max_state_dim=4,
            max_action_dim=4, resize_imgs_with_padding=(32, 32), expert_width_multiplier=1.,
            load_vlm_weights=False, freeze_vision_encoder=False, train_expert_only=False)
        cls = api.SmolVLAPolicy
    else:
        from transformers import PaliGemmaConfig, SiglipVisionConfig
        original_mapping = api.CONFIG_MAPPING
        def small_paligemma():
            return PaliGemmaConfig(vision_config=SiglipVisionConfig(hidden_size=32, intermediate_size=64,
                num_hidden_layers=1, num_attention_heads=4, image_size=32, patch_size=8).to_dict())
        monkeypatch.setattr(api, 'CONFIG_MAPPING', {'paligemma': small_paligemma, 'gemma': original_mapping['gemma']})
        monkeypatch.setattr(api, 'get_gemma_config', lambda *a: api.GemmaConfig(32, 2, 64, 8, 2, 4))
        cls = api.PI0Policy if family == 'pi0' else api.PI05Policy
        cfg = cls.config_class(device='cpu', chunk_size=6, n_action_steps=6,
            max_state_dim=4, max_action_dim=4, image_resolution=(32, 32), dtype='float32')
    cfg.input_features, cfg.output_features = feature_schema({'joint_names': ['a', 'b'], 'image_shapes': {'top': [32, 32, 3]}})
    policy = cls(cfg)
    if family != 'smolvla':
        policy.model.paligemma_with_expert.paligemma.model.multi_modal_projector.linear = torch.nn.Linear(32, 32)
    batch = {'observation.state': torch.zeros(2, 2), 'observation.images.top': torch.rand(2, 3, 32, 32),
             'observation.language.tokens': torch.tensor([[2, 3, 4], [2, 3, 4]]),
             'observation.language.attention_mask': torch.ones(2, 3, dtype=torch.bool),
             'action': torch.randn(2, 6, 2), 'action_is_pad': torch.tensor([[False]*5+[True], [False]*6])}
    return policy, LeRobotFlow(policy, family), batch


def test_uniform_time_matches_original_velocity_and_cached_sampling(tiny_flow):
    policy, adapter, batch = tiny_flow
    policy.eval()
    with torch.no_grad():
        encoded = adapter.encode(batch, cache=True)
        x = torch.randn(2, 6, 4); t = torch.full((2,), .6)
        kw = dict(prefix_pad_masks=encoded['pad'], past_key_values=encoded['cache'], x_t=x, timestep=t)
        if adapter.family == 'pi0':
            kw['state'] = encoded['state']
        original = adapter.model.denoise_step(**kw)
        adapted = adapter.velocity(x, t[:, None].expand(2, 6), encoded)
        torch.testing.assert_close(adapted, original, atol=1e-5, rtol=1e-4)
        cached = adapter.sample(batch, 3, 'standard')
    assert cached.shape == (2, 6, 2) and torch.isfinite(cached).all()


def test_clean_prefix_training_gradients_and_rtc_inference(tiny_flow):
    policy, adapter, batch = tiny_flow
    policy.train()
    loss = adapter.loss(batch, 2, clean_prefix=True)
    loss.backward()
    assert torch.isfinite(loss) and policy.model.action_out_proj.weight.grad.abs().sum() > 0
    policy.eval()
    for p in policy.parameters():
        p.requires_grad_(False)
    previous = torch.full((2, 4, 2), .25)
    guided = adapter.sample(batch, 3, 'rtc', previous, 2)
    trained = adapter.sample(batch, 3, 'training_time_rtc', previous, 2)
    assert torch.isfinite(guided).all()
    torch.testing.assert_close(trained[:, :2], previous[:, :2])

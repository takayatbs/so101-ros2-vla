"""Real XVLA and GR00T action heads, with synthetic VLM features."""
from types import SimpleNamespace

import pytest

torch = pytest.importorskip('torch')
pytest.importorskip('lerobot.policies.factory')

from so101_core.xvla_flow import XVLAFlow, transformer_tokens
from so101_core.groot_flow import GrootFlow, action_tokens, dit_tokens


def test_xvla_original_endpoint_per_token_time_training_and_rtc():
    from lerobot.policies.xvla.soft_transformer import SoftPromptedTransformer
    from lerobot.policies.xvla.action_hub import AutoActionSpace
    torch.set_num_threads(2)
    head = SoftPromptedTransformer(hidden_size=32, multi_modal_input_size=32, depth=2, num_heads=4,
        num_domains=1, dim_action=4, dim_propio=2, dim_time=8, len_soft_prompts=2, max_len_seq=32)
    model = SimpleNamespace(transformer=head, chunk_size=6, dim_action=4,
        action_space=AutoActionSpace(2, 4), _get_target_dtype=lambda: torch.float32)
    inputs = {'domain_id': torch.zeros(2, dtype=torch.long), 'proprio': torch.zeros(2, 2),
              'vlm_features': torch.randn(2, 3, 32), 'aux_visual_inputs': torch.randn(2, 2, 32)}
    model.forward_vlm = lambda *args: {k: inputs[k] for k in ('vlm_features', 'aux_visual_inputs')}
    policy = SimpleNamespace(model=model, config=SimpleNamespace(action_feature=SimpleNamespace(shape=(2,))),
        _build_model_inputs=lambda batch: {**inputs, 'input_ids': None, 'image_input': torch.zeros(2, 1), 'image_mask': None},
        _prepare_action_targets=lambda batch: torch.nn.functional.pad(batch['action'], (0, 2)))
    adapter = XVLAFlow(policy)
    x = torch.randn(2, 6, 4); t = torch.full((2,), .6)
    head.eval()
    torch.testing.assert_close(transformer_tokens(model, inputs, x, t[:, None].expand(-1, 6)),
        head(**inputs, action_with_noise=x, t=t))
    batch = {'action': torch.randn(2, 6, 2), 'action_is_pad': torch.zeros(2, 6, dtype=torch.bool)}
    head.train(); loss = adapter.loss(batch, 2, True); loss.backward()
    assert torch.isfinite(loss) and any(p.grad is not None and p.grad.abs().sum() > 0 for p in head.parameters())
    head.eval(); head.requires_grad_(False)
    prefix = torch.full((2, 4, 2), .25)
    assert torch.isfinite(adapter.sample(batch, 3, 'rtc', prefix, 2)).all()
    result = adapter.sample(batch, 3, 'training_time_rtc', prefix, 2)
    torch.testing.assert_close(result[:, :2], prefix[:, :2])


@pytest.mark.parametrize('alternate', [False, True])
def test_groot_original_velocity_token_times_training_and_rtc(alternate):
    from transformers.feature_extraction_utils import BatchFeature
    from lerobot.policies.groot.groot_n1_7 import GR00TN17Config, GR00TN17ActionHead
    torch.set_num_threads(2)
    config = GR00TN17Config(hidden_size=32, input_embedding_dim=32, backbone_embedding_dim=32,
        max_state_dim=4, max_action_dim=4, max_num_embodiments=1, action_horizon=6, state_dropout_prob=0.,
        use_alternate_vl_dit=alternate, vl_self_attention_cfg={'num_layers': 0},
        diffusion_model_cfg={'num_layers': 2, 'num_attention_heads': 4, 'attention_head_dim': 8,
            'norm_type': 'ada_norm', 'dropout': 0., 'output_dim': 32, 'interleave_self_attention': True,
            'compute_dtype': 'float32'})
    head = GR00TN17ActionHead(config)
    class Network(torch.nn.Module):
        def __init__(self):
            super().__init__(); self.action_head = head
        def prepare_input(self, values):
            action = torch.nn.functional.pad(values['action'], (0, 2))
            return None, BatchFeature({'state': values['observation.state'][:, None], 'action': action,
                'action_mask': torch.tensor([True, True, False, False])[None, None].expand(2, 6, 4),
                'embodiment_id': torch.zeros(2, dtype=torch.long)})
        def backbone(self, values):
            return BatchFeature({'backbone_features': torch.ones(2, 3, 32),
                'backbone_attention_mask': torch.ones(2, 3, dtype=torch.bool),
                'image_mask': torch.tensor([[True, False, False]]*2)})
    network = Network()
    policy = SimpleNamespace(_groot_model=network, config=SimpleNamespace(use_bf16=False,
        action_feature=SimpleNamespace(shape=(2,))), _filter_groot_inputs=lambda batch, **kw: batch)
    adapter = GrootFlow(policy)
    batch = {'observation.state': torch.zeros(2, 4), 'action': torch.randn(2, 6, 2),
             'action_is_pad': torch.zeros(2, 6, dtype=torch.bool)}
    network.eval()
    encoded = adapter.encode(batch)
    x = torch.randn(2, 6, 4); times = torch.full((2, 6), .6)
    ids = encoded['action_input'].embodiment_id
    buckets = ((1-times)*head.num_timestep_buckets).long()
    tokens = action_tokens(head, x, buckets, ids)
    torch.testing.assert_close(tokens, head.action_encoder(x, buckets[:, 0], ids))
    hidden = torch.cat([encoded['state'], tokens], 1)
    all_times = torch.cat([buckets[:, :1], buckets], 1)
    kw = {} if not alternate else {'image_mask': encoded['backbone'].image_mask,
                                  'backbone_attention_mask': encoded['backbone'].backbone_attention_mask}
    torch.testing.assert_close(dit_tokens(head.model, hidden, encoded['backbone'], all_times),
        head.model(hidden, encoded['backbone'].backbone_features, timestep=buckets[:, 0], **kw), atol=1e-5, rtol=1e-4)
    network.train(); loss = adapter.loss(batch, 2, True); loss.backward()
    assert torch.isfinite(loss) and any(p.grad is not None and p.grad.abs().sum() > 0 for p in head.parameters())
    network.eval(); network.requires_grad_(False)
    prefix = torch.full((2, 4, 2), .25)
    assert torch.isfinite(adapter.sample(batch, 3, 'rtc', prefix, 2)).all()
    trained = adapter.sample(batch, 3, 'training_time_rtc', prefix, 2)
    torch.testing.assert_close(trained[:, :2, :2], prefix[:, :2])

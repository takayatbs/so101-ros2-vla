"""Project ACT variant: shared CNN + Transformer, CVAE action-chunk training.

Architecture reference: Zhao et al., https://arxiv.org/abs/2304.13705
This implementation/weight format is independent of LeRobot and the ACT repo.
"""
import json
from pathlib import Path
import torch
from torch import nn

FORMAT = "so101_act_v1"


class ACT(nn.Module):
    def __init__(self, config):
        super().__init__()
        d, joints, latent = config["dim_model"], len(config["joint_names"]), config["latent_dim"]
        heads, ff, dropout = config["n_heads"], config["dim_feedforward"], config["dropout"]
        if d % heads or config["chunk_size"] < 1 or not config["cameras"]:
            raise ValueError("Invalid ACT dimensions/cameras")
        self.config = config
        self.cnn = nn.Sequential(nn.Conv2d(3, 32, 5, stride=2, padding=2), nn.ReLU(),
                                 nn.Conv2d(32, 64, 3, stride=2, padding=1), nn.ReLU(),
                                 nn.Conv2d(64, d, 3, stride=2, padding=1), nn.ReLU(),
                                 nn.AdaptiveAvgPool2d((4, 4)))
        self.state_projection, self.latent_projection = nn.Linear(joints, d), nn.Linear(latent, d)
        token_count = 2+16*len(config["cameras"])
        self.observation_position = nn.Parameter(torch.randn(1, token_count, d)*.02)
        self.query = nn.Parameter(torch.randn(1, config["chunk_size"], d)*.02)
        encoder_layer = nn.TransformerEncoderLayer(d, heads, ff, dropout, batch_first=True, norm_first=True)
        self.encoder = nn.TransformerEncoder(encoder_layer, config["n_encoder_layers"], enable_nested_tensor=False)
        decoder_layer = nn.TransformerDecoderLayer(d, heads, ff, dropout, batch_first=True, norm_first=True)
        self.decoder = nn.TransformerDecoder(decoder_layer, config["n_decoder_layers"])
        self.action_head = nn.Linear(d, joints)
        self.posterior_cls = nn.Parameter(torch.randn(1, 1, d)*.02)
        self.posterior_position = nn.Parameter(torch.randn(1, config["chunk_size"]+2, d)*.02)
        self.action_projection = nn.Linear(joints, d)
        posterior_layer = nn.TransformerEncoderLayer(d, heads, ff, dropout, batch_first=True, norm_first=True)
        self.posterior = nn.TransformerEncoder(posterior_layer, config["n_vae_encoder_layers"], enable_nested_tensor=False)
        self.latent_head = nn.Linear(d, 2*latent)

    def forward(self, state, images, actions=None, padding=None):
        batch = state.shape[0]
        mean = logvar = None
        if actions is not None:
            tokens = torch.cat((self.posterior_cls.expand(batch, -1, -1),
                                self.state_projection(state).unsqueeze(1), self.action_projection(actions)), dim=1)
            mask = torch.cat((torch.zeros(batch, 2, dtype=torch.bool, device=state.device), padding), dim=1)
            encoded = self.posterior(tokens+self.posterior_position, src_key_padding_mask=mask)[:, 0]
            mean, logvar = self.latent_head(encoded).chunk(2, dim=-1)
            logvar = logvar.clamp(-10, 10)
            z = mean+(logvar*.5).exp()*torch.randn_like(mean)
        else:
            z = torch.zeros(batch, self.config["latent_dim"], device=state.device)
        visual = [self.cnn(images[name]).flatten(2).transpose(1, 2) for name in self.config["cameras"]]
        tokens = torch.cat((self.state_projection(state).unsqueeze(1),
                            self.latent_projection(z).unsqueeze(1), *visual), dim=1)
        memory = self.encoder(tokens+self.observation_position)
        prediction = self.action_head(self.decoder(self.query.expand(batch, -1, -1), memory))
        return prediction, mean, logvar


def act_loss(prediction, actions, padding, mean, logvar, kl_weight):
    valid = (~padding).unsqueeze(-1)
    reconstruction = ((prediction-actions).abs()*valid).sum()/(valid.sum()*actions.shape[-1]).clamp_min(1)
    kl = -.5*(1+logvar-mean.square()-logvar.exp()).sum(dim=-1).mean()
    return reconstruction+kl_weight*kl, reconstruction, kl


def save_checkpoint(folder, model, statistics, training, metrics):
    from safetensors.torch import save_file
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    save_file({k: v.detach().cpu().contiguous() for k, v in model.state_dict().items()}, str(folder/"model.safetensors"))
    for name, value in [("config.json", model.config), ("normalization.json", statistics),
                        ("train_config.json", training), ("metrics.json", metrics)]:
        (folder/name).write_text(json.dumps(value, indent=2))


def load_checkpoint(folder, device):
    from safetensors.torch import load_file
    folder = Path(folder)
    config = json.loads((folder/"config.json").read_text())
    if config.get("format") != FORMAT:
        raise ValueError("SO101 ACT v1 required; LeRobot checkpoints require retraining")
    model = ACT(config)
    model.load_state_dict(load_file(str(folder/"model.safetensors")), strict=True)
    model.to(device).eval()
    statistics = json.loads((folder/"normalization.json").read_text())
    return model, statistics

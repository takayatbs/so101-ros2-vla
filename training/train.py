#!/usr/bin/env python3
"""Train the project ACT CVAE/Transformer and register its native checkpoint."""
import argparse
import json
import random
from pathlib import Path
import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset
from so101_core.act import ACT, FORMAT, act_loss, save_checkpoint
from so101_core.config import load_system, config_hash, calibration_hashes
from so101_core.preprocessing import ImagePreprocessor
from so101_core.dataset import SO101Dataset
from so101_core.model_registry import register


class TrainingDataset(Dataset):
    def __init__(self, dataset, statistics):
        self.dataset = dataset
        self.stats = {k: torch.tensor(v, dtype=torch.float32) for k, v in statistics.items()}

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, index):
        frame = self.dataset[index]
        return {"state": (torch.from_numpy(frame["state"])-self.stats["state_mean"])/self.stats["state_std"],
                "action": (torch.from_numpy(frame["action"])-self.stats["action_mean"])/self.stats["action_std"],
                "padding": torch.from_numpy(frame["padding"]),
                "images": {n: torch.from_numpy(v).permute(2, 0, 1).float()/255 for n, v in frame["images"].items()}}


def to_device(batch, device):
    return {k: {n: v.to(device) for n, v in value.items()} if isinstance(value, dict) else value.to(device)
            for k, value in batch.items()}


def main():
    parser = argparse.ArgumentParser(description="Local SO101 ACT training (no LeRobot dependency)")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--config", default="/workspace/config/runtime/system.real30hz.yaml")
    parser.add_argument("--steps", type=int, default=20000)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--seed", type=int, default=42)
    for name in ("dim-model", "n-heads", "dim-feedforward", "n-encoder-layers", "n-decoder-layers", "n-vae-encoder-layers", "num-workers"):
        parser.add_argument("--"+name, type=int)
    args = parser.parse_args()
    if min(args.steps, args.batch_size) < 1:
        raise ValueError("Steps and batch size must be positive")
    config = load_system(args.config)
    dataset_root = Path(args.dataset).resolve()
    provenance = json.loads((dataset_root/"provenance.json").read_text())
    if provenance["observation_hash"] != config_hash(config["observation"]) or provenance["units"] != "rad" or provenance["joint_names"] != config["robot"]["joints"]:
        raise ValueError("Dataset schema/preprocessing does not match runtime")
    if provenance["robot_hash"] != config_hash(config["robot"]):
        raise ValueError("Dataset robot settings differ; use its configuration or reconvert original MCAP")
    if provenance.get("calibration_sha256", {}) != calibration_hashes(config["robot"]):
        raise ValueError("Calibration changed since dataset conversion")
    settings = {k: v for k, v in config["training"].items() if not k.startswith("_")}
    for name in ("dim_model", "n_heads", "dim_feedforward", "n_encoder_layers", "n_decoder_layers", "n_vae_encoder_layers", "num_workers"):
        if getattr(args, name) is not None:
            settings[name] = getattr(args, name)
    if not 0 <= settings["validation_fraction"] < 1 or min(settings["eval_every"], settings["save_every"]) < 1:
        raise ValueError("Invalid validation/checkpoint settings")
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.device.startswith("cuda") and not torch.cuda.is_available():
        raise ValueError("CUDA is unavailable; specify --device cpu or check container GPU")
    # Hold out whole episodes; adjacent frames/chunks never cross the split.
    full = SO101Dataset(dataset_root, config["policy"]["action_chunk"]["chunk_size"])
    total_rows = sum(e['frames'] for e in full.info['episodes'])
    quality = {'target_rows':total_rows,'usable_rows':len(full),'excluded_rows':total_rows-len(full),
               'warning_rows':sum(int(table.get('warning',np.zeros(len(table['state']),dtype=bool)).sum()) for table in full.tables.values())}
    print('Dataset synchronization:',json.dumps(quality),flush=True)
    preprocessor = ImagePreprocessor(config["observation"])
    if full.info["image_shapes"] != {n: list(preprocessor.shape(n)) for n in config["observation"]["cameras"]} or full.info["joint_names"] != config["robot"]["joints"]:
        raise ValueError("Dataset image/joint schema differs from runtime")
    if len({e["task"] for e in full.info["episodes"]}) != 1:
        raise ValueError("This ACT variant is single-task; build a separate dataset/model per task")
    indices = list(full.tables)
    random.Random(args.seed).shuffle(indices)
    n_val = min(len(indices)-1, max(1, round(len(indices)*settings["validation_fraction"]))) if len(indices) > 1 and settings["validation_fraction"] > 0 else 0
    validation_indices, train_indices = indices[:n_val], indices[n_val:]
    train = SO101Dataset(dataset_root, full.chunk_size, train_indices)
    stats = train.statistics()
    loader = DataLoader(TrainingDataset(train, stats), batch_size=args.batch_size, shuffle=True,
                        num_workers=settings["num_workers"], generator=torch.Generator().manual_seed(args.seed))
    val_loader = None
    if validation_indices:
        validation = SO101Dataset(dataset_root, full.chunk_size, validation_indices)
        val_loader = DataLoader(TrainingDataset(validation, stats), batch_size=args.batch_size, num_workers=settings["num_workers"])
    model_config = {k: settings[k] for k in ("dim_model", "n_heads", "dim_feedforward", "n_encoder_layers", "n_decoder_layers", "n_vae_encoder_layers", "latent_dim", "dropout")}
    model_config.update(format=FORMAT, chunk_size=full.chunk_size, joint_names=full.info["joint_names"],
                        cameras=config["observation"]["cameras"], image_shapes=full.info["image_shapes"], task=full.info["episodes"][0]["task"])
    model = ACT(model_config).to(args.device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=settings["learning_rate"], weight_decay=settings["weight_decay"])
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError("Choose a new output directory; training is never overwritten")
    output.mkdir(parents=True)
    training = {**vars(args), "settings": settings, "train_episodes": train_indices,
                "validation_episodes": validation_indices, "dataset_format": full.info["format"],
                "dataset_quality":quality,"synchronization":full.info.get('synchronization',{})}
    (output/"train_config.json").write_text(json.dumps(training, indent=2))
    iterator, metrics, checkpoint = iter(loader), {}, None
    with (output/"metrics.jsonl").open("x") as log:
        for step in range(1, args.steps+1):
            try:
                batch = next(iterator)
            except StopIteration:
                iterator = iter(loader)
                batch = next(iterator)
            batch = to_device(batch, args.device)
            model.train()
            prediction, mean, logvar = model(batch["state"], batch["images"], batch["action"], batch["padding"])
            loss, reconstruction, kl = act_loss(prediction, batch["action"], batch["padding"], mean, logvar, settings["kl_weight"])
            if not torch.isfinite(loss):
                raise ValueError("Nonfinite training loss; model was not registered")
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), settings["grad_clip"], error_if_nonfinite=True)
            optimizer.step()
            metrics = {"step": step, "loss": float(loss.detach()), "reconstruction_l1": float(reconstruction.detach()), "kl": float(kl.detach()), "validation_l1_rad": None}
            if val_loader is not None and (step % settings["eval_every"] == 0 or step == args.steps):
                model.eval()
                numerator, denominator = 0., 0
                with torch.inference_mode():
                    for val in val_loader:
                        val = to_device(val, args.device)
                        pred, _, _ = model(val["state"], val["images"])
                        valid = (~val["padding"]).unsqueeze(-1)
                        std = torch.tensor(stats["action_std"], device=args.device)
                        numerator += float(((pred-val["action"]).abs()*std*valid).sum())
                        denominator += int(valid.sum())*len(full.info["joint_names"])
                metrics["validation_l1_rad"] = numerator/denominator
            log.write(json.dumps(metrics)+"\n")
            log.flush()
            if step == 1 or step % 100 == 0 or step == args.steps:
                print(json.dumps(metrics), flush=True)
            if step % settings["save_every"] == 0 or step == args.steps:
                checkpoint = output/f"checkpoints/{step:06d}"
                save_checkpoint(checkpoint, model, stats, training, metrics)
    if val_loader is None:
        print("エピソードが1本のため検証用データはありません。実機性能の判断には使用できません。")
    model_path = register(checkpoint, dataset_root, args.model_id, config)
    print("Registered model:", model_path)


if __name__ == "__main__":
    main()

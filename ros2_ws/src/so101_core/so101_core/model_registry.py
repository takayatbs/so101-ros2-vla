import argparse
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .config import load_system, config_hash, resolve
from .converter import sha256_file
from .preprocessing import ImagePreprocessor
from .dataset import SUPPORTED_FORMATS


def register(checkpoint, dataset, model_id, config):
    checkpoint, dataset = Path(checkpoint).resolve(), Path(dataset).resolve()
    if not (checkpoint/"model.safetensors").is_file():
        raise ValueError("Expected a SO101 checkpoint with model.safetensors")
    for name in ("config.json", "normalization.json", "train_config.json", "metrics.json"):
        if not (checkpoint/name).is_file():
            raise ValueError("Missing checkpoint file: "+name)
    provenance = json.loads((dataset/"provenance.json").read_text())
    joints = config["robot"]["joints"]
    if provenance["observation_hash"] != config_hash(config["observation"]):
        raise ValueError("Dataset and runtime preprocessing differ")
    if provenance["joint_names"] != joints or provenance["units"] != "rad":
        raise ValueError("Dataset has an incompatible joint/units schema")
    metadata_file = 'so101_adapter.json' if (checkpoint/'so101_adapter.json').is_file() else 'config.json'
    policy = json.loads((checkpoint/metadata_file).read_text())
    if policy.get("format") not in ("so101_act_v1", "so101_flow_v1", "so101_lerobot_v1"):
        raise ValueError("SO101 ACT, flow or LeRobot adapter checkpoint required")
    if provenance.get("dataset_format_version") not in (*SUPPORTED_FORMATS, 'lerobot_v3.0'):
        raise ValueError("Supported SO101 dataset required")
    if provenance.get("robot_hash") != config_hash(config["robot"]):
        raise ValueError("Dataset and runtime robot settings differ")
    if policy["joint_names"] != joints or policy["cameras"] != config["observation"]["cameras"]:
        raise ValueError("Checkpoint joint/camera schema mismatch")
    if policy["chunk_size"] != config["policy"]["action_chunk"]["chunk_size"]:
        raise ValueError("Checkpoint/runtime action chunk length differs")
    preprocessing = ImagePreprocessor(config["observation"])
    if policy["image_shapes"] != {n: list(preprocessing.shape(n)) for n in config["observation"]["cameras"]}:
        raise ValueError("Checkpoint image shape mismatch")
    for role, expected in provenance.get("calibration_sha256", {}).items():
        if sha256_file(resolve(config["robot"], config["robot"][role]["calibration"])) != expected:
            raise ValueError("Calibration changed since dataset conversion: "+role)
    statistics = json.loads((checkpoint/"normalization.json").read_text())
    import numpy as np
    for key in ("state_mean", "state_std", "action_mean", "action_std"):
        values = np.asarray(statistics[key])
        if values.shape != (len(joints),) or not np.isfinite(values).all() or (key.endswith("std") and (values <= 0).any()):
            raise ValueError("Invalid normalization statistics: "+key)
    root = resolve(config["policy"], config["policy"]["model_root"]).resolve()
    target = (root/model_id).resolve()
    if not target.is_relative_to(root) or target == root or target.exists():
        raise ValueError("Choose a new model_id under model_root")
    root.mkdir(parents=True, exist_ok=True)
    stage = root/(".partial-"+uuid.uuid4().hex)
    try:
        shutil.copytree(checkpoint, stage/"checkpoint")
        shutil.copy2(dataset/"provenance.json", stage/"dataset_provenance.json")
        train_config = checkpoint/"train_config.json"
        training = json.loads(train_config.read_text()) if train_config.exists() else {}
        manifest = {
            "model_id": model_id, "model_version": model_id, "runtime_version": "0.2.0",
            "docker_image": os.environ.get("SO101_IMAGE", "unknown"),
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "model_format": policy['format'], "dataset_format": provenance['dataset_format_version'],
            "robot_hash": provenance["robot_hash"],
            "calibration_sha256": provenance.get("calibration_sha256", {}),
            "dataset_version": provenance["dataset_version"], "dataset_root": str(dataset),
            "dataset_provenance_sha256": sha256_file(dataset/"provenance.json"),
            "observation_hash": provenance["observation_hash"], "joint_names": joints,
            "synchronization":provenance.get('synchronization_config',{}),
            "units": "rad", "training_seed": training.get("seed"),
            "training_config": training, "metrics": json.loads((checkpoint/"metrics.json").read_text()),
            "checkpoint_sha256": {str(p.relative_to(stage/"checkpoint")): sha256_file(p)
                                   for p in (stage/"checkpoint").rglob("*") if p.is_file()},
        }
        # Preserve the exact scheduler/observation profile used for training.
        # Component paths still point at the installed robot configuration;
        # startup hashes reject later incompatible camera/calibration changes.
        runtime = stage/'runtime'
        runtime.mkdir()
        files = {name: value['_file'] for name, value in config.items()}
        backend = {'so101_act_v1':'act', 'so101_flow_v1':'flow', 'so101_lerobot_v1':'lerobot'}[policy['format']]
        saved_policy = {key: value for key, value in config['policy'].items() if not key.startswith('_')}
        saved_policy.update(backend=backend, model_id=model_id, model_root=str(root))
        profiles = {}
        for correction in ('none', 'a2c2'):
            suffix = '' if correction == 'none' else '.a2c2'
            # Round-trip via JSON to avoid mutating the caller's nested config.
            spec = json.loads(json.dumps(saved_policy))
            spec['action_chunk']['correction'] = {'method':correction, 'checkpoint':''}
            (runtime/f'policy{suffix}.yaml').write_text(json.dumps(spec, indent=2))
            profile_files = {**files, 'policy':f'policy{suffix}.yaml'}
            filename = f'system{suffix}.yaml'
            (runtime/filename).write_text(json.dumps({'files':profile_files}, indent=2))
            profiles[correction] = 'runtime/'+filename
        manifest['runtime_profiles'] = profiles
        (stage/"deployment.json").write_text(json.dumps(manifest, indent=2))
        stage.rename(target)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--config", default="/workspace/config/runtime/system.yaml")
    args = parser.parse_args()
    print(register(args.checkpoint, args.dataset, args.model_id, load_system(args.config)))


if __name__ == "__main__":
    main()

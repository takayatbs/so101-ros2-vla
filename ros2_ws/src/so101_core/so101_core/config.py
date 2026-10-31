from pathlib import Path
import hashlib
import json
import os
import yaml


def config_group(filename):
    """Canonical purpose folder; also used to resolve recorded legacy paths."""
    filename = filename.replace('.local.yaml', '.yaml')
    for prefix, folder in (("system", "runtime"), ("camera", "cameras"),
                           ("record", "collection"), ("dataset", "dataset"),
                           ("observation", "observation"), ("policy", "policy"),
                           ("training", "training")):
        if filename.startswith(prefix) and filename.endswith('.yaml'):
            return folder
    return {"robot.yaml":"hardware", "controllers.yaml":"hardware",
            "teleop.yaml":"control", "safety.yaml":"control"}.get(filename)


def config_path(path):
    """Prefer the requested file; resolve old flat paths without symlink clutter."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        group = config_group(path.name)
        candidate = path.parent/group/path.name if group else path
        if candidate.is_file():
            return candidate
    return path


def configuration_root(path):
    path = Path(path)
    if path.parent.name == config_group(path.name) and (path.parent.parent/'runtime').is_dir():
        return path.parent.parent
    return path.parent


def load_config(path):
    path = config_path(path)
    # A local file replaces the complete public profile. Keeping it beside the
    # original preserves relative resources and the effective configuration hash.
    local = local_config_path(path)
    if os.environ.get('SO101_USE_LOCAL_CONFIG', '1') != '0' and local.is_file():
        path = local
    with path.open() as stream:
        config = yaml.safe_load(stream)
    config["_root"] = str(path.parent)
    config["_file"] = str(path)
    config["_config_root"] = str(configuration_root(path))
    config["_resource_prefix"] = os.path.relpath(path.parent, config["_config_root"])
    return config


def local_config_path(path):
    """Private destination for machine-specific settings and calibration output."""
    path = Path(path)
    return path if path.name.endswith('.local.yaml') else path.with_name(path.stem+'.local.yaml')


def config_hash(config):
    cleaned = {key: value for key, value in config.items() if not key.startswith("_")}
    # Resource paths keep their original config-root-relative representation in
    # fingerprints. Moving YAML files must not invalidate existing recordings or
    # trained models; changing a resource/name/value still changes its identity.
    if '_resource_prefix' in config:
        path_keys = {'calibration', 'bag_root', 'dataset_root', 'model_root'}
        def canonical(value, key=None):
            if isinstance(value, dict):
                return {k:canonical(v,k) for k,v in value.items()}
            if isinstance(value, list):
                return [canonical(v) for v in value]
            if key in path_keys and isinstance(value, str) and not Path(value).is_absolute():
                return os.path.normpath(os.path.join(config['_resource_prefix'], value))
            return value
        cleaned = canonical(cleaned)
    return hashlib.sha256(json.dumps(cleaned, sort_keys=True).encode()).hexdigest()


def resolve(config, path):
    p = Path(path)
    return p if p.is_absolute() else Path(config["_root"]) / p


def load_system(path):
    manifest = load_config(path)
    config = {name: load_config(resolve(manifest, file)) for name, file in manifest["files"].items()}
    if 'controllers' not in config:
        # Older manifests did not list the controller file explicitly.
        candidate = config_path(Path(manifest['_config_root'])/'controllers.yaml')
        if candidate.is_file():
            config['controllers'] = load_config(candidate)
    return config


def calibration_hashes(robot):
    result = {}
    for role in ("leader", "follower"):
        path = resolve(robot, robot[role]["calibration"])
        if path.is_file():
            result[role] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result

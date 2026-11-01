"""Resolve a registered model to its runtime profile; never enable AUTONOMOUS."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='Deploy any registered model using its saved method')
    parser.add_argument('model_id')
    parser.add_argument('backend', nargs='?', choices=['mock', 'mujoco', 'real'], default='mock')
    parser.add_argument('--correction', help='Trained A2C2 directory (inside the container, e.g. /workspace/data/...)')
    parser.add_argument('--config', help='Override runtime system YAML (container path)')
    parser.add_argument('--model-root', help='Registration root on this machine')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--async-inference', action='store_true',
                        help='Use the asynchronous scheduler for standard policies too; never enable AUTONOMOUS')
    parser.add_argument('--smoothing', choices=['none', 'three_point'], default='none',
                        help='Smooth arm goals; gripper goals remain unchanged (default: none)')
    args, extra = parser.parse_known_args()
    root = Path(__file__).resolve().parents[1]
    models = Path(args.model_root or root/'data/models').resolve()
    folder = (models/args.model_id).resolve()
    if not folder.is_relative_to(models) or folder == models:
        parser.error('model-id must stay under model-root')
    manifest = json.loads((folder/'deployment.json').read_text())
    method = manifest['training_config'].get('method', 'standard')
    if method not in ('standard', 'rtc', 'training_time_rtc', 'vlash'):
        parser.error('Unknown checkpoint method: '+method)
    suffix = method + ('.a2c2' if args.correction else '')
    profile = args.config or f'/workspace/config/runtime/system.real30hz.640x480.{suffix}.yaml'
    if args.config is None and manifest.get('runtime_profiles'):
        profile_path = (folder/manifest['runtime_profiles']['a2c2' if args.correction else 'none']).resolve()
        if not profile_path.is_relative_to(folder):
            parser.error('Saved runtime profile must stay under the registered model')
        if not profile_path.is_file():
            parser.error('Saved runtime profile is missing: '+str(profile_path))
        profile = ('/workspace/'+str(profile_path.relative_to(root))) if profile_path.is_relative_to(root) else str(profile_path)
    family = manifest['model_format']
    if family == 'so101_act_v1' and args.correction:
        parser.error('A2C2 requires the flow or LeRobot adapter; use train --policy act for a LeRobot ACT model')
    backend = {'so101_lerobot_v1': 'lerobot', 'so101_flow_v1': 'flow', 'so101_act_v1': 'act'}[family]
    command = [str(root/'scripts/runtime.sh'), args.backend, f'policy_backend:={backend}',
               f'model_id:={args.model_id}', f'config:={profile}']
    if family == 'so101_lerobot_v1':
        command.append('model_python:=/workspace/data/envs/policies/bin/python')
    if args.correction:
        command.append('correction_checkpoint:='+args.correction)
    if args.smoothing != 'none':
        command.append('action_smoothing:='+args.smoothing)
    if args.async_inference:
        command.append('asynchronous_inference:=true')
    command.extend(extra)
    if args.dry_run:
        print(json.dumps(command, ensure_ascii=False))
    else:
        os.execv(command[0], command)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Generate synthetic regression vectors using pinned upstream normalization code."""
import argparse
import ast
from enum import Enum
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

REFERENCE_COMMIT = '22bd7a2f489b367d8df42de803b1e8c4ca63a3f9'
JOINTS = ('shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper')


class MotorNormMode(str, Enum):
    RANGE_0_100 = 'range_0_100'
    RANGE_M100_100 = 'range_m100_100'
    DEGREES = 'degrees'


def generate(source):
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != REFERENCE_COMMIT:
        raise ValueError('Use the pinned upstream LeRobot commit: '+REFERENCE_COMMIT)
    # Execute only the upstream arithmetic methods; no bus, SDK or device is opened.
    tree = ast.parse((source/'src/lerobot/motors/motors_bus.py').read_text())
    methods = [node for cls in tree.body if isinstance(cls, ast.ClassDef)
               for node in cls.body if isinstance(node, ast.FunctionDef)
               and node.name in ('_normalize', '_unnormalize')]
    if len(methods) != 2:
        raise ValueError('Upstream normalization methods were not found')
    scope = {'MotorNormMode': MotorNormMode}
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])), '<upstream-normalization>', 'exec'), scope)
    cases = []
    for role_index, role in enumerate(('leader', 'follower')):
        raw = {joint: dict(id=i+1, drive_mode=0, homing_offset=(i-2)*100,
                          range_min=500+i*40, range_max=3500-i*30-role_index*100)
               for i, joint in enumerate(JOINTS)}
        raw['wrist_roll'].update(range_min=0, range_max=4095)
        raw['gripper'].update(range_min=1200, range_max=2200+role_index*400)
        for use_degrees in (True, False):
            ids = {i+1: name for i, name in enumerate(JOINTS)}
            bus = SimpleNamespace(calibration={k: SimpleNamespace(**v) for k, v in raw.items()},
                                  apply_drive_mode=False, model_resolution_table={'synthetic': 4096},
                                  _id_to_name=ids.__getitem__, _id_to_model=lambda _: 'synthetic')
            bus.motors = {name: SimpleNamespace(norm_mode=(MotorNormMode.RANGE_0_100 if name == 'gripper'
                          else MotorNormMode.DEGREES if use_degrees else MotorNormMode.RANGE_M100_100))
                          for name in JOINTS}
            case = dict(role=role, use_degrees=use_degrees, calibration=raw, read=[], write=[])
            for kind in ('zero', 'below', 'low', 'mid', 'high', 'above', 'full'):
                values = {}
                for name, item in raw.items():
                    lo, hi = item['range_min'], item['range_max']
                    values[name] = {'zero': 0, 'below': lo-1, 'low': lo, 'mid': (lo+hi)//2,
                                    'high': hi, 'above': hi+1, 'full': 4095}[kind]
                result = scope['_normalize'](bus, {raw[k]['id']: v for k, v in values.items()})
                case['read'].append({'raw': values, 'native': {ids[k]: v for k, v in result.items()}})
            for value in (-200., -100., -0.5, 0., 0.5, 20., 50., 100., 200.):
                values = dict.fromkeys(JOINTS, value)
                result = scope['_unnormalize'](bus, {raw[k]['id']: v for k, v in values.items()})
                case['write'].append({'native': values, 'raw': {ids[k]: v for k, v in result.items()}})
            cases.append(case)
    return dict(reference_commit=REFERENCE_COMMIT, calibration_source='synthetic; no physical robot data', cases=cases)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lerobot-source', type=Path, help='Optional source override; defaults to the pinned in-project cache')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1]/'tests/fixtures/lerobot_so101_reference.json')
    args = parser.parse_args()
    source = args.lerobot_source
    if source is None:
        from install_policy_env import prepare_source
        source = prepare_source()
    args.output.write_text(json.dumps(generate(source), indent=2)+'\n')

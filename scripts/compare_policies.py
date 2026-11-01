#!/usr/bin/env python3
"""Plan and summarize a standard/VLASH x smoothing comparison; never move a robot."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import shlex
import statistics


ROOT = Path(__file__).resolve().parents[1]
CONDITIONS = [(method+'_'+smoothing, method, smoothing)
              for method in ('standard', 'vlash') for smoothing in ('none', 'three_point')]
METRICS = ('duration_s', 'start_delay_s', 'inference_p50_ms', 'inference_p95_ms',
           'measured_arm_jerk_rms_rad_s3')


def read(path):
    return json.loads(Path(path).read_text())


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def registered(root, model_id, expected_method):
    folder = (Path(root)/model_id).resolve()
    if not folder.is_relative_to(Path(root).resolve()) or folder == Path(root).resolve():
        raise ValueError('Model ID must stay under model-root')
    if not (folder/'deployment.json').is_file():
        return None
    if not (folder/'checkpoint/model.safetensors').is_file():
        raise ValueError('Registered checkpoint weights are missing: '+model_id)
    manifest = read(folder/'deployment.json')
    training = read(folder/'checkpoint/train_config.json')
    adapter = folder/'checkpoint/so101_adapter.json'
    bridge = read(adapter if adapter.is_file() else folder/'checkpoint/config.json')
    if training['method'] != expected_method or bridge['objective'] != expected_method:
        raise ValueError('Checkpoint method differs from condition: '+model_id)
    if manifest['training_config'] != training:
        raise ValueError('Registered training metadata differs from checkpoint: '+model_id)
    profile = (folder/manifest['runtime_profiles']['none']).resolve()
    if not profile.is_relative_to(folder):
        raise ValueError('Runtime profile leaves model folder')
    system = read(profile)
    policy_path = (profile.parent/system['files']['policy']).resolve()
    if not policy_path.is_relative_to(folder):
        raise ValueError('Runtime policy leaves model folder')
    policy = read(policy_path)
    chunk = policy['action_chunk']
    if chunk['scheduler']['method'] != expected_method or chunk['correction']['method'] != 'none':
        raise ValueError('Comparison requires matching methods without A2C2')
    comparable = {
        'training': {k: training[k] for k in (
            'policy', 'seed', 'steps', 'dataset_signature', 'train_episodes',
            'validation_episodes', 'training_frames', 'batch_size', 'policy_overrides')},
        'model': {k: bridge[k] for k in (
            'chunk_size', 'joint_names', 'cameras',
            'image_shapes', 'task', 'fps', 'max_delay_steps', 'denoise_steps', 'action_semantics')},
        'resources': {k: manifest[k] for k in (
            'robot_hash', 'calibration_sha256', 'observation_hash', 'synchronization', 'units')},
        'normalization': manifest['checkpoint_sha256']['normalization.json'],
        'runtime': {'inference_rate_hz': policy['inference_rate_hz'],
                    'chunk_size': chunk['chunk_size'], 'n_action_steps': chunk['n_action_steps'],
                    'max_delay_steps': chunk['scheduler']['max_delay_steps'],
                    'max_inference_s': chunk['scheduler']['max_inference_s']},
    }
    if bridge.get('format') == 'so101_flow_v1' and not adapter.is_file():
        comparable['model'].update(policy_family='so101_flow_v1', pretrained_source='from scratch',
            architecture={k: bridge[k] for k in ('dim_model', 'n_heads', 'n_layers', 'dropout', 'max_guidance_weight')})
    else:
        comparable['model'].update({k: bridge[k] for k in ('policy_family', 'pretrained_source')})
    return {'model_id': model_id, 'method': expected_method,
            'checkpoint_sha256': manifest['checkpoint_sha256'], 'comparable': comparable}


def validate(plan):
    models = {method: registered(plan['model_root'], model_id, method)
              for method, model_id in plan['models'].items()}
    pending = [method for method, model in models.items() if model is None]
    present = [m for m in models.values() if m is not None]
    if len(present) == 2 and present[0]['comparable'] != present[1]['comparable']:
        differing = [k for k in present[0]['comparable']
                     if present[0]['comparable'][k] != present[1]['comparable'][k]]
        raise ValueError('Confounded comparison; settings differ: '+', '.join(differing))
    return {'ready': not pending, 'pending_methods': pending, 'models': models}


def prepare(output, standard_model, vlash_model, model_root, repetitions=30, seed=42,
            max_duration_s=30., success_rule='Object fully inside the box and released, without intervention.',
            allow_pending=False):
    if repetitions < 1 or not math.isfinite(max_duration_s) or max_duration_s <= 0:
        raise ValueError('Positive trial count and time limit required')
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    rng = random.Random(seed)
    trials = []
    for block in range(1, repetitions+1):
        order = CONDITIONS.copy()
        rng.shuffle(order)
        for condition, method, smoothing in order:
            trials.append({'trial_id': len(trials)+1, 'block': block,
                           'condition': condition, 'method': method, 'smoothing': smoothing})
    plan = {'format': 'so101_four_conditions_v1', 'created_at': datetime.now(timezone.utc).isoformat(),
            'models': {'standard': standard_model, 'vlash': vlash_model},
            'model_root': str(Path(model_root).resolve()), 'trials_per_condition': repetitions,
            'order_seed': seed, 'max_duration_s': max_duration_s, 'success_rule': success_rule,
            'execution': 'asynchronous for all conditions; correction disabled; smoothing inference-only',
            'denominator': 'all recorded attempts, including failures and aborts; unrun trials reported separately',
            'trials': trials}
    status = validate(plan)
    if not status['ready'] and not allow_pending:
        raise ValueError('Models not registered yet: '+', '.join(status['pending_methods']))
    output.mkdir(parents=True)
    write_new(output/'plan.json', plan)
    with (output/'trial-order.csv').open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['trial_id', 'block', 'condition', 'method', 'smoothing'])
        writer.writeheader(); writer.writerows(trials)
    return status


def trial(plan, trial_id):
    match = [t for t in plan['trials'] if t['trial_id'] == trial_id]
    if not match:
        raise ValueError('Unknown trial ID')
    return match[0]


def command(plan, trial_id, backend='mock'):
    if not validate(plan)['ready']:
        raise ValueError('Wait for both models to be registered before evaluation')
    item = trial(plan, trial_id)
    return ['./scripts/deploy_policy.sh', plan['models'][item['method']], backend,
            '--smoothing', item['smoothing'], '--async-inference']


def record(folder, trial_id, outcome, values, notes='', recording=None):
    folder = Path(folder); plan = read(folder/'plan.json'); item = trial(plan, trial_id)
    status = validate(plan)
    if not status['ready']:
        raise ValueError('Both registered models are required before recording results')
    if outcome not in ('success', 'failure', 'aborted'):
        raise ValueError('Unknown outcome')
    if values.get('duration_s') is None:
        raise ValueError('Actual elapsed duration is required for every attempt')
    for key, value in values.items():
        if key not in METRICS or (value is not None and (not math.isfinite(value) or value < 0)):
            raise ValueError('Invalid measurement: '+key)
    if outcome == 'success' and values['duration_s'] > plan['max_duration_s']:
        raise ValueError('An attempt exceeding the time limit cannot be marked successful')
    if values.get('start_delay_s') is not None and values['start_delay_s'] > values['duration_s']:
        raise ValueError('Start delay exceeds attempt duration')
    if (values.get('inference_p50_ms') is not None and values.get('inference_p95_ms') is not None
            and values['inference_p50_ms'] > values['inference_p95_ms']):
        raise ValueError('Inference p50 exceeds p95')
    snapshot = folder/'models.json'
    identity = {'plan_sha256': hashlib.sha256((folder/'plan.json').read_bytes()).hexdigest(),
                'models': status['models']}
    if snapshot.exists():
        if read(snapshot) != identity:
            raise ValueError('Model identity, protocol or settings changed during comparison')
    else:
        write_new(snapshot, identity)
    row = {**item, 'outcome': outcome, **values,
           'recorded_at': datetime.now(timezone.utc).isoformat(),
           'notes': notes, 'recording': recording}
    write_new(folder/'trials'/f'{trial_id:04d}.json', row)
    return row


def wilson(successes, count):
    if not count:
        return None
    z = 1.959963984540054; p = successes/count; denominator = 1+z*z/count
    center = (p+z*z/(2*count))/denominator
    width = z*math.sqrt(p*(1-p)/count+z*z/(4*count*count))/denominator
    return [max(0., center-width), min(1., center+width)]


def summarize(folder, output):
    folder, output = Path(folder), Path(output)
    if output.exists():
        raise FileExistsError(output)
    plan = read(folder/'plan.json')
    rows = [read(p) for p in sorted((folder/'trials').glob('*.json'))]
    identity = read(folder/'models.json') if (folder/'models.json').is_file() else None
    if rows and (identity is None or identity['plan_sha256'] != hashlib.sha256((folder/'plan.json').read_bytes()).hexdigest()):
        raise ValueError('Recorded protocol differs from current plan')
    seen = set()
    for row in rows:
        expected = trial(plan, row['trial_id'])
        if row['trial_id'] in seen or any(row[k] != v for k, v in expected.items()):
            raise ValueError('Duplicate or inconsistent trial record')
        if row['outcome'] not in ('success', 'failure', 'aborted'):
            raise ValueError('Invalid outcome')
        seen.add(row['trial_id'])
    summaries = []
    for condition, method, smoothing in CONDITIONS:
        selected = [r for r in rows if r['condition'] == condition]
        successes = [r for r in selected if r['outcome'] == 'success']
        measurements = {}
        for key in METRICS:
            values = [r[key] for r in selected if r.get(key) is not None]
            measurements[key] = {'median': statistics.median(values) if values else None,
                                 'measured_attempts': len(values)}
        summaries.append({'condition': condition, 'method': method, 'smoothing': smoothing,
            'planned': plan['trials_per_condition'], 'attempts': len(selected),
            'successes': len(successes), 'failures': sum(r['outcome'] == 'failure' for r in selected),
            'aborted': sum(r['outcome'] == 'aborted' for r in selected),
            'unrecorded': plan['trials_per_condition']-len(selected),
            'success_rate': len(successes)/len(selected) if selected else None,
            'success_rate_ci95': wilson(len(successes), len(selected)),
            'successful_duration_median_s': statistics.median([r['duration_s'] for r in successes]) if successes else None,
            'measurements': measurements})
    result = {'format': plan['format'], 'models': plan['models'],
              'complete': len(rows) == len(plan['trials']), 'success_rule': plan['success_rule'],
              'max_duration_s': plan['max_duration_s'], 'denominator': plan['denominator'],
              'execution': plan['execution'], 'order_seed': plan['order_seed'],
              'plan_sha256': hashlib.sha256((folder/'plan.json').read_bytes()).hexdigest(),
              'model_evidence': identity['models'] if identity else None,
              'conditions': summaries,
              'limitations': ['One trained seed per method; trial confidence intervals do not measure training-seed variation.',
                              'Human reset between attempts; no intervention during a successful attempt.',
                              'Missing measurements remain null; prediction smoothness alone does not establish task success.']}
    output.mkdir(parents=True)
    write_new(output/'results.json', result)
    fields = ['trial_id', 'block', 'condition', 'method', 'smoothing', 'outcome', *METRICS]
    with (output/'trials.csv').open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)
    lines = ['# Standard / VLASH × smoothing', '',
             'Status: '+('complete' if result['complete'] else 'incomplete; unrecorded trials are not successes or failures'), '',
             '| Condition | Success / attempts | 95% CI | Aborted | Unrecorded | Success duration median (s) |',
             '|---|---|---|---|---|---|']
    for entry in summaries:
        ci = entry['success_rate_ci95']
        interval = f'{ci[0]*100:.1f}–{ci[1]*100:.1f}%' if ci else '—'
        duration = entry['successful_duration_median_s']
        duration = f'{duration:.2f}' if duration is not None else '—'
        lines.append(f'| {entry["condition"]} | {entry["successes"]} / {entry["attempts"]} | '
                     f'{interval} | {entry["aborted"]} | {entry["unrecorded"]} | {duration} |')
    lines += ['', 'Failures and aborts both count in the success-rate denominator.', '',
              'Single trained seed per method. See results.json for measurements and limitations.',
              'Raw recording paths and free-text notes are intentionally omitted from this export.', '']
    (output/'results.md').write_text('\n'.join(lines))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare')
    for name in ('standard-model', 'vlash-model', 'output'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--model-root', type=Path, default=ROOT/'data/models')
    p.add_argument('--trials-per-condition', type=int, default=30)
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--max-duration-s', type=float, default=30.)
    p.add_argument('--success-rule', default='Object fully inside the box and released, without intervention.')
    p.add_argument('--allow-pending', action='store_true')
    for name in ('check', 'command', 'record', 'summarize'):
        p = sub.add_parser(name); p.add_argument('experiment', type=Path)
        if name in ('command', 'record'):
            p.add_argument('--trial-id', type=int, required=True)
        if name == 'command':
            p.add_argument('--backend', choices=['mock', 'real', 'mujoco'], default='mock')
        elif name == 'record':
            p.add_argument('--outcome', choices=['success', 'failure', 'aborted'], required=True)
            for metric in METRICS:
                p.add_argument('--'+metric.replace('_', '-'), type=float, required=metric=='duration_s')
            p.add_argument('--notes', default=''); p.add_argument('--recording')
        elif name == 'summarize':
            p.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        status = prepare(args.output, args.standard_model, args.vlash_model, args.model_root,
                         args.trials_per_condition, args.seed, args.max_duration_s,
                         args.success_rule, args.allow_pending)
        print(json.dumps({'ready': status['ready'], 'pending_methods': status['pending_methods']}))
    elif args.action == 'check':
        status = validate(read(args.experiment/'plan.json'))
        print(json.dumps({'ready': status['ready'], 'pending_methods': status['pending_methods']}))
    elif args.action == 'command':
        print(shlex.join(command(read(args.experiment/'plan.json'), args.trial_id, args.backend)))
    elif args.action == 'record':
        row = record(args.experiment, args.trial_id, args.outcome,
                     {key: getattr(args, key) for key in METRICS}, args.notes, args.recording)
        print(json.dumps({'trial_id': row['trial_id'], 'outcome': row['outcome']}))
    else:
        result = summarize(args.experiment, args.output)
        print(json.dumps({'complete': result['complete'], 'output': str(args.output)}))


if __name__ == '__main__':
    main()

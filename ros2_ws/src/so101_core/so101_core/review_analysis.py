"""Transparent review metrics and episode comparison; no automatic task labels."""
from collections import Counter
import hashlib
import json

import numpy as np


def segments(mask):
    start = None
    for i, value in enumerate([*mask, False]):
        if value and start is None:
            start = i
        elif not value and start is not None:
            yield start, i
            start = None


def percentile(values, p=95):
    values = [v for v in values if v is not None and np.isfinite(v)]
    return float(np.percentile(values, p)) if values else None


def enrich(data, model=None, metadata=None, timing=None):
    metadata = metadata or {}
    rows = data['rows']
    if hasattr(model, 'evaluate'):
        try:
            model.evaluate(data['joints'], np.zeros(len(data['joints'])))
        except (ValueError, KeyError) as error:
            model = {'available': False, 'accuracy': '記録された関節ではEEを計算できません', 'reason': str(error)}
    description = model.description if hasattr(model, 'description') else model or {'available': False, 'accuracy': 'EEモデル未指定'}
    data['kinematics'] = description
    data['success'] = metadata.get('success', data.get('success'))
    data['accepted'] = metadata.get('collection_selection', {}).get('accepted', data.get('accepted'))
    data['failure_reason'] = metadata.get('failure_reason', data.get('failure_reason', ''))
    data['provenance'] = {key: metadata.get(key, data.get('provenance', {}).get(key)) for key in ('robot_hash', 'calibration_sha256', 'control_mode_at_start')}
    data['timing'] = timing if timing is not None else data.get('timing')
    for row in rows:
        row['ee'] = model.evaluate(data['joints'], row.get('state')) if hasattr(model, 'evaluate') else None
        target = model.evaluate(data['joints'], row.get('action')) if hasattr(model, 'evaluate') else None
        row['ee_target'] = target['position'] if target else None
        state, action = row.get('state'), row.get('action')
        row['joint_error'] = None if state is None or action is None else [None if a is None or b is None else b-a for a, b in zip(state, action)]
        row['ee_speed'] = None
    valid = [bool(r['valid']) for r in rows]
    for start, end in segments([r['valid'] and r['ee'] is not None for r in rows]):
        for i in range(start+1, end):
            dt = rows[i]['t']-rows[i-1]['t']
            if dt > 0:
                rows[i]['ee_speed'] = float(np.linalg.norm(np.subtract(rows[i]['ee']['position'], rows[i-1]['ee']['position']))/dt)
    duration = rows[-1]['t']-rows[0]['t'] if rows else 0
    origin_ns = int(data.get('origin_timestamp_ns', rows[0]['timestamp_ns']))
    start_ns = metadata.get('start_time_ns')
    end_ns = metadata.get('end_time_ns')
    if start_ns is None and metadata.get('start_time') is not None:
        start_ns = round(metadata['start_time']*1e9)
    if end_ns is None and metadata.get('end_time') is not None:
        end_ns = round(metadata['end_time']*1e9)
    source_duration = (end_ns-start_ns)/1e9 if start_ns is not None and end_ns is not None and end_ns>start_ns else None
    head_s = max(0., (origin_ns-start_ns)/1e9) if start_ns is not None else None
    tail_s = max(0., (end_ns-origin_ns)/1e9-rows[-1]['t']) if end_ns is not None else None
    data['origin_timestamp_ns'] = str(origin_ns)
    data['source_start_time_ns'] = str(start_ns) if start_ns is not None else None
    normalized = [None]*101
    length = 0.
    for start, end in segments([r['valid'] and r['ee'] is not None for r in rows]):
        times = np.array([r['t'] for r in rows[start:end]])
        positions = np.array([r['ee']['position'] for r in rows[start:end]])
        if end-start > 1:
            length += float(np.linalg.norm(np.diff(positions, axis=0), axis=1).sum())
            for i in range(101):
                query = rows[0]['t']+duration*i/100
                if times[0] <= query <= times[-1]:
                    normalized[i] = [float(np.interp(query, times, positions[:, axis])) for axis in range(3)]
    available = [r for r in rows if r['valid'] and r['ee']]
    reasons = Counter(reason for row in rows for reason in row.get('reasons', []))
    issues = []
    if head_s is not None and head_s > .1:
        issues.append({'t':0., 'end':0., 'kind':'coverage', 'label':f'原記録の先頭 {head_s:.3f}s は共通の対応範囲外'})
    if tail_s is not None and tail_s > .1:
        issues.append({'t':rows[-1]['t'], 'end':rows[-1]['t'], 'kind':'coverage', 'label':f'原記録の末尾 {tail_s:.3f}s は共通の対応範囲外'})
    for start, end in segments([not r['valid'] for r in rows]):
        issues.append({'t': rows[start]['t'], 'end': rows[end-1]['t'], 'kind': 'excluded',
                       'label': '同期条件外: '+', '.join(sorted({reason for r in rows[start:end] for reason in r.get('reasons', [])}))})
    # Report motion outliers as review candidates, never label them task failures.
    speeds = [r['ee_speed'] for r in rows if r['ee_speed'] is not None]
    if speeds:
        median = float(np.median(speeds)); mad = float(np.median(np.abs(np.asarray(speeds)-median)))
        threshold = max(.25, median+6*1.4826*mad)
        for start, end in segments([r['ee_speed'] is not None and r['ee_speed'] > threshold for r in rows]):
            issues.append({'t': rows[start]['t'], 'end': rows[end-1]['t'], 'kind': 'motion',
                           'label': 'EE速度の確認候補（閾値 '+f'{threshold:.2f}'+' m/s）'})
    else:
        threshold = None
    remaining = 0; chunk_lengths = []
    for row in reversed(rows):
        remaining = remaining+1 if row['valid'] else 0
        if remaining:
            chunk_lengths.append(min(30, remaining))
    camera_stats = {}
    for name, video in data['videos'].items():
        assignments = [r.get('image_index', {}).get(name) for r in rows if r['valid']]
        repeats = sum(a['image_index'].get(name) == b['image_index'].get(name) for a,b in zip(rows,rows[1:])
                      if a['valid'] and b['valid'] and a.get('image_index', {}).get(name) is not None and b.get('image_index', {}).get(name) is not None)
        camera_stats[name] = {**video.get('stats', {}), 'repeated_assignments': repeats,
                              'repeat_percent': 100*repeats/max(1, len(assignments)-1),
                              'age_p95_ms': percentile([abs(r.get('errors', {}).get('image/'+name))*1000 for r in rows if r['valid'] and r.get('errors', {}).get('image/'+name) is not None])}
    finite_errors = [abs(v) for r in rows if r['valid'] for v in (r.get('joint_error') or []) if v is not None]
    metrics = {'duration_s': duration, 'rows': len(rows), 'valid_rows': sum(valid),
               'source_duration_s': source_duration, 'head_unaligned_s':head_s, 'tail_unaligned_s':tail_s,
               'source_coverage_percent': min(100.,100*duration/source_duration) if source_duration else None,
               'valid_percent': 100*sum(valid)/max(1, len(rows)), 'warning_rows': sum(bool(r.get('warnings')) for r in rows),
               'excluded_rows': len(rows)-sum(valid), 'reasons': dict(reasons), 'cameras': camera_stats,
               'joint_error_p95_rad': percentile(finite_errors),
               'ee_path_length_m': length if available else None,
               'ee_speed_p95_m_s': percentile(speeds), 'ee_speed_review_threshold_m_s': threshold,
               'ee_start': available[0]['ee']['position'] if available else None,
               'ee_end': available[-1]['ee']['position'] if available else None,
               'chunk_30_full_percent': 100*sum(n == 30 for n in chunk_lengths)/max(1, len(chunk_lengths)),
               'chunk_30_padding_percent': 100*sum(30-n for n in chunk_lengths)/max(1, 30*len(chunk_lengths))}
    data.update(metrics=metrics, issues=sorted(issues, key=lambda i: i['t']), normalized_ee=normalized)
    return data


def summary(data, source=None, index=0):
    provenance = data.get('provenance', {})
    # Unknown recording provenance must not be merged into a calibrated cohort.
    identity = {'task': data.get('task'), 'model': data.get('kinematics'),
                'robot': provenance.get('robot_hash'), 'calibration': provenance.get('calibration_sha256')}
    group = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    return {'id': data['episode'], 'task': data['task'], 'source': str(source or ''),
            'time_origin_timestamp_ns':data['origin_timestamp_ns'], 'source_start_time_ns':data.get('source_start_time_ns'),
            'success': data.get('success'), 'accepted': data.get('accepted'), 'group': group,
            'group_label': data['task']+' / '+group[:6], 'metrics': data['metrics'],
            'normalized_ee': data['normalized_ee'],
            'trajectory': [{'t': r['t'], 'p': r['ee']['position'] if r.get('ee') else None, 'valid': r['valid']}
                           for r in data['rows']],
            'data_script': f'episodes/{index:06d}/review_data.js',
            'review': f'episodes/{index:06d}/review.html', 'latency': f'episodes/{index:06d}/latency.html',
            'frames': data['metrics']['rows'], 'valid_frames': data['metrics']['valid_rows']}


def consistency(items):
    """Progress-aligned spread; only comparable accepted/successful episodes form the reference."""
    for item in items:
        item['consistency_rms_m'] = None
        item['endpoint_distance_m'] = None
        item['startpoint_distance_m'] = None
        peers = [i for i in items if i['group'] == item['group'] and i.get('accepted') is not False and i.get('success') is not False]
        if len(peers) < 2:
            continue
        own = item['normalized_ee']
        distances = []
        for j in range(101):
            # Leave the current episode out so its own deviation is not diluted.
            other = [i['normalized_ee'][j] for i in peers if i['id'] != item['id'] and i['normalized_ee'][j] is not None]
            if own[j] is not None and other:
                distances.append(float(np.linalg.norm(np.asarray(own[j])-np.mean(other, axis=0))))
        if distances:
            item['consistency_rms_m'] = float(np.sqrt(np.mean(np.square(distances))))
        other_ends = [i['metrics']['ee_end'] for i in peers if i['id'] != item['id'] and i['metrics']['ee_end'] is not None]
        if item['metrics']['ee_end'] is not None and other_ends:
            item['endpoint_distance_m'] = float(np.linalg.norm(np.asarray(item['metrics']['ee_end'])-np.mean(other_ends, axis=0)))
        other_starts = [i['metrics']['ee_start'] for i in peers if i['id'] != item['id'] and i['metrics']['ee_start'] is not None]
        if item['metrics']['ee_start'] is not None and other_starts:
            item['startpoint_distance_m'] = float(np.linalg.norm(np.asarray(item['metrics']['ee_start'])-np.mean(other_starts, axis=0)))

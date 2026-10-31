"""Portable, offline review workspace with shared playback and 3D trajectories."""
import json
from pathlib import Path
import shutil

import numpy as np

from .review_analysis import enrich, summary, consistency


ASSETS = Path(__file__).with_name('review_assets')


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).replace('<', r'\u003c')


def review_data(folder, metadata, rows, videos, joints):
    origin = rows[0]['target_ns']
    indices = {}
    video_data = {}
    for name, spec in videos.items():
        with np.load(folder/'videos'/spec['index_path'], allow_pickle=False) as archive:
            indices[name] = {key: archive[key].copy() for key in archive.files}
        stamps = indices[name].get('timestamp_ns', np.array([], dtype=np.int64))
        in_range = stamps[(stamps >= rows[0]['target_ns']) & (stamps <= rows[-1]['target_ns'])]
        intervals = np.diff(in_range)/1e6
        stats = {'fps': float((len(in_range)-1)*1e9/(in_range[-1]-in_range[0])) if len(in_range)>1 else None,
                 'max_interval_ms': float(intervals.max()) if len(intervals) else None,
                 'gaps_over_50ms': int((intervals > 50).sum()) if len(intervals) else None,
                 'stored_frames': len(indices[name]['pts_ns'])}
        video_data[name] = {'path': 'videos/'+spec['path'],
                            'origin_s': (spec['timestamp_origin_ns']-origin)/1e9, 'stats': stats}
    def vector(sample):
        return None if sample is None else [float(x) if np.isfinite(x) else None for x in sample.value]
    return {'episode': metadata['episode_id'], 'task': metadata['task_id'], 'joints': list(joints),
        'origin_timestamp_ns': str(origin), 'videos': video_data,
        'rows': [{'t': (r['target_ns']-origin)/1e9, 'timestamp_ns': str(r['target_ns']),
                  'valid': bool(r['valid']), 'warnings': r['warnings'], 'reasons': r['reasons'],
                  'state': vector(r['state']), 'action': vector(r['action']), 'errors': r['errors'],
                  'image_t': {n: None if s is None else (s.timestamp_ns-origin)/1e9 for n,s in r['images'].items()},
                  'image_pts': {n: None if s is None else int(indices[n]['pts_ns'][s.value])/1e9 for n,s in r['images'].items()},
                  'image_index': {n: None if s is None else int(s.value) for n,s in r['images'].items()}}
                 for r in rows]}


def write_page(folder, workspace, initial=None):
    folder = Path(folder)
    assets = folder/'assets'
    assets.mkdir(exist_ok=True)
    for name in ('review.css', 'review.js'):
        shutil.copyfile(ASSETS/name, assets/name)
    template = (ASSETS/'review.html').read_text(encoding='utf-8')
    page = template.replace('__WORKSPACE__', json_text(workspace)).replace('__INITIAL__', json_text(initial))
    (folder/'index.html').write_text(page, encoding='utf-8')
    return folder/'index.html'


def write_episode_data(folder, data):
    folder = Path(folder)
    workspace = {'format': 'so101_review_workspace_v2', 'title': data['task'],
                 'kinematics': data['kinematics'], 'episodes': [summary(data)], 'single': True}
    write_page(folder, workspace, data)
    (folder/'index.html').replace(folder/'review.html')
    payload = 'window.SO101ReviewEpisodes=window.SO101ReviewEpisodes||{};window.SO101ReviewEpisodes['+json_text(data['episode'])+']='+json_text(data)+';\n'
    (folder/'review_data.js').write_text(payload, encoding='utf-8')
    return data


def write_review(folder, metadata, rows, videos, joints, model=None, timing=None):
    data = review_data(Path(folder), metadata, rows, videos, joints)
    enrich(data, model, metadata, timing)
    return write_episode_data(folder, data)


def write_workspace(folder, items, model, title='Recording Review', initial=None):
    consistency(items)
    workspace = {'format': 'so101_review_workspace_v2', 'title': title,
                 'kinematics': model.description if hasattr(model, 'description') else model,
                 'episodes': items, 'single': False}
    write_page(folder, workspace, initial)
    # The manifest is also a machine-readable inspection index.
    (Path(folder)/'workspace.json').write_text(json_text(workspace), encoding='utf-8')
    return workspace

"""Read existing review bundles or Dataset v2 without decoding/re-encoding videos."""
import copy
import json
import os
from pathlib import Path
import re
import shutil

import numpy as np

from .review import review_data
from .synchronization import Sample


def local_child(base, relative):
    base = Path(base).resolve()
    path = (base/relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError('Review file path escapes its input directory: '+str(relative))
    return path

def copy_videos(source, target, videos):
    target.mkdir(parents=True, exist_ok=True)
    for name, spec in videos.items():
        path = local_child(source, spec['path'])
        if not path.is_file():
            raise ValueError('Video not found: '+str(path))
        destination = target/(name+'.mp4')
        try:
            os.link(path, destination)
        except OSError:
            shutil.copy2(path, destination)
        spec['path'] = 'videos/'+destination.name

def read_review_data(page_path):
    page_path = Path(page_path)
    page = page_path.read_text(encoding='utf-8')
    match = re.search(r'<script\b(?=[^>]*\bid=["\']data["\'])[^>]*>(.*?)</script>', page, re.S)
    if not match:
        raise ValueError('Embedded review data not found: '+str(page_path))
    data = json.loads(match.group(1))
    if not isinstance(data, dict) or not data.get('rows'):
        raise ValueError('No rows in existing review: '+str(page_path))
    return data


def read_existing_episode(root, item, output):
    page_path = local_child(root, item['review'])
    data = read_review_data(page_path)
    if data['episode'] != item.get('episode_id', item.get('id')):
        raise ValueError('Review manifest and episode ID disagree')
    metadata = {'episode_id': data['episode'], 'task_id': data['task'], 'success': item.get('success'),
                'collection_selection': {'accepted': item.get('accepted')}}
    source = Path(item.get('source', ''))
    if source.is_dir() and (source/'episode.json').is_file():
        original = json.loads((source/'episode.json').read_text())
        if original['episode_id'] != data['episode']:
            raise ValueError('Original metadata and review ID disagree')
        metadata.update(original)
        if (source/'collection.json').is_file():
            metadata['collection_selection'] = json.loads((source/'collection.json').read_text())
    timing_file = page_path.parent/'latency.json'
    timing = json.loads(timing_file.read_text()) if timing_file.exists() else data.get('timing')
    copy_videos(page_path.parent, output/'videos', data['videos'])
    return data, metadata, timing

def read_dataset_episode(root, item, output):
    base = local_child(root, f"episodes/{item['index']:06d}")
    saved = json.loads((base/'source_episode.json').read_text()) if (base/'source_episode.json').exists() else {}
    metadata = saved.get('episode', {'episode_id': item['episode_id'], 'task_id': item['task']})
    with np.load(base/'frames.npz', allow_pickle=False) as archive:
        table = {key: archive[key].copy() for key in archive.files}
    joints = json.loads((root/'dataset.json').read_text())['joint_names']
    rows = []
    annotations = [json.loads(line) for line in (base/'alignment.jsonl').read_text().splitlines()] if (base/'alignment.jsonl').exists() else []
    names = list(item['videos'])
    for i, stamp in enumerate(table['timestamp_ns']):
        def sample(key):
            values = table[key][i]
            if not np.isfinite(values).all():
                return None
            return Sample(float(stamp)/1e9, values, int(table.get(key+'_time_ns', table['timestamp_ns'])[i]))
        errors = {key: float(table[key+'_time_error'][i]) if np.isfinite(table[key+'_time_error'][i]) else None for key in ('state', 'action')}
        images = {}
        for name in names:
            index = int(table['image_index/'+name][i])
            images[name] = Sample(float(stamp)/1e9, index, int(table['image_time_ns/'+name][i])) if index >= 0 else None
            error = float(table['image_time_error/'+name][i])
            errors['image/'+name] = error if np.isfinite(error) else None
        row_annotation = annotations[i] if i < len(annotations) else {}
        rows.append({'target_ns': int(stamp), 'valid': bool(table.get('valid', np.ones(len(table['timestamp_ns']), dtype=bool))[i]),
            'warnings': row_annotation.get('warnings', ['recorded_warning'] if table.get('warning', np.zeros(len(table['timestamp_ns']), dtype=bool))[i] else []),
            'reasons': row_annotation.get('reasons', []), 'state': sample('state'), 'action': sample('action'), 'errors': errors, 'images': images})
    videos = copy.deepcopy(item['videos'])
    data = review_data(base, metadata, rows, videos, joints)
    copy_videos(base, output/'videos', data['videos'])
    return data, metadata, None

"""Compare camera-only hardware benchmarks without rerunning the hardware."""
import argparse
import gzip
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def recorded_series(report):
    result = {}
    with gzip.open(Path(report['bag'])/'raw/message_index.jsonl.gz', 'rt') as stream:
        for line in stream:
            item = json.loads(line)
            if item['topic_name'].endswith('/video'):
                name = item['topic_name'].split('/')[2]
                result.setdefault(name, []).append((item['source_timestamp_ns'],
                    (item['receive_timestamp_ns']-item['source_timestamp_ns'])/1e6))
    # Remove decoder preroll using the measured frame count. The live-window
    # boundary may differ by one sample; JSON percentiles are authoritative.
    measured = {name: items[-report['cameras'][name]['frames']:] for name, items in result.items()}
    return {name: (np.array([row[0] for row in rows], dtype=np.int64),
                   np.array([row[1] for row in rows], dtype=float)) for name, rows in measured.items()}


def camera_alignment(series, cameras):
    stamps = {name: series[name][0] for name in cameras}
    start = max(s[0] for s in stamps.values()); end = min(s[-1] for s in stamps.values())
    targets = np.array([start+round(i*1e9/30) for i in range(int((end-start)*30/1e9)+1)], dtype=np.int64)
    chosen = np.array([s[np.searchsorted(s, targets, side='right')-1] for s in stamps.values()])
    ages_ms = (targets-chosen)/1e6
    skew_ms = (chosen.max(axis=0)-chosen.min(axis=0))/1e6
    valid = (ages_ms.max(axis=0) <= 50)&(skew_ms <= 50)
    return {'scope': 'Camera constraints only, 30Hz previous selection; no robot state/action measured',
            'cameras': cameras, 'rows': len(valid), 'excluded_rows': int((~valid).sum()),
            'excluded_percent': float((~valid).mean()*100), 'max_source_age_ms': float(ages_ms.max()),
            'max_camera_skew_ms': float(skew_ms.max())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--soak', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    before = json.loads(args.before.read_text()); after = json.loads(args.after.read_text())
    fig, axes = plt.subplots(3, 2, figsize=(13, 9), sharey=True)
    comparison = {'before': str(args.before), 'after': str(args.after), 'cameras': {}, 'alignment': {}}
    for column, (label, report) in enumerate((('Before', before), ('After', after))):
        series = recorded_series(report)
        for row, name in enumerate(('top', 'side', 'arm')):
            values = series[name]
            x = (values[0]-values[0][0])/1e9
            axes[row, column].plot(x, values[1], linewidth=.7)
            axes[row, column].set_title(f'{label}: {name}')
            axes[row, column].set_xlabel('Seconds'); axes[row, column].set_ylabel('Recorder receive delay (ms)')
            axes[row, column].grid(alpha=.25)
        comparison['alignment'][label.lower()] = {'top_arm': camera_alignment(series, ['top', 'arm']),
                                                   'all_three': camera_alignment(series, ['top', 'side', 'arm'])}
    for name in before['cameras']:
        old = before['cameras'][name]; new = after['cameras'][name]
        comparison['cameras'][name] = {'before': old, 'after': new,
            'median_reduction_percent': 100*(1-new['receive_delay_ms']['p50']/old['receive_delay_ms']['p50'])}
    if args.soak:
        comparison['soak'] = json.loads(args.soak.read_text())
    fig.tight_layout(); fig.savefig(args.output/'before_after.png', dpi=150)
    (args.output/'comparison.json').write_text(json.dumps(comparison, ensure_ascii=False, indent=2))
    print(json.dumps(comparison['alignment'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

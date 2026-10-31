"""Generate portable video/joint and recorder timing reviews from recorded episodes."""
import argparse
import copy
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from functools import partial
import gzip
import json
import math
import multiprocessing
import os
from pathlib import Path
import signal
import shutil
import statistics
import sys
import tempfile
import time
import uuid

import numpy as np

from .config import load_system
from .converter import read_episode
from .review import review_data, write_episode_data, write_workspace
from .review_analysis import enrich, summary
from .review_kinematics import load_review_kinematics
from .review_import import read_existing_episode, read_dataset_episode, read_review_data
from .synchronization import aligned_samples, alignment_settings


PROJECT = Path(__file__).resolve().parents[4]


def sources(path, accepted_only=False):
    """Accept an episode, its raw MCAP, or a collection directory/session.json."""
    path = Path(path).expanduser().resolve()
    if path.suffix == '.mcap':
        if not path.is_file():
            raise ValueError('MCAP not found: '+str(path))
        path = path.parent.parent
    if path.name == 'episode.json':
        path = path.parent
    session = path/'session.json' if path.is_dir() else path
    if session.is_file() and session.name == 'session.json':
        data = json.loads(session.read_text())
        if data.get('format') != 'so101_collection_v1':
            raise ValueError('Unsupported collection format')
        if data.get('status') in ('starting', 'running', 'recording'):
            raise ValueError('Finish collection before generating a review')
        paths = data.get('accepted_episodes', []) if accepted_only else [a['path'] for a in data['attempts']]
        folders = list(dict.fromkeys(Path(p).resolve() for p in paths))
        if not folders:
            raise ValueError('No episodes in this collection')
        return folders, data.get('runtime_config'), session.parent.name
    if path.is_dir() and (path/'episode.json').is_file():
        return [path], None, path.name
    raise ValueError('Specify an episode directory, episode.json, raw/*.mcap, or collection session')


def timing_report(folder):
    """Measure only the actual episode window, excluding decoder preroll/latched data."""
    metadata = json.loads((folder/'episode.json').read_text())
    start = metadata.get('start_time_ns', round(metadata['start_time']*1e9))
    end = metadata.get('end_time_ns', round(metadata['end_time']*1e9))
    index = folder/'raw/message_index.jsonl.gz'
    if not index.exists():
        index = folder/'raw/message_index.jsonl'
    result = {'episode_id': metadata['episode_id'], 'topics': {}, 'index_available': index.exists(),
              'start_time_ns': str(start), 'meaning': 'Recorder receive time minus source timestamp. Not exposure-to-display or physical response latency.'}
    if not index.exists():
        return result
    with (gzip.open(index, 'rt', encoding='utf-8') if index.suffix == '.gz' else index.open()) as stream:
        for line in stream:
            item = json.loads(line)
            stamp = item.get('source_timestamp_ns')
            if stamp is None or not start <= stamp <= end:
                continue
            topic = result['topics'].setdefault(item['topic_name'], {'samples': [], 'timestamp_semantics': item.get('timestamp_semantics', '')})
            topic['samples'].append([(stamp-start)/1e9, (item['receive_timestamp_ns']-stamp)/1e6])
    for topic in result['topics'].values():
        samples = topic['samples']
        delays = sorted(row[1] for row in samples)
        gaps = [(b[0]-a[0])*1000 for a, b in zip(samples, samples[1:])]
        topic.update(messages=len(samples), median_ms=statistics.median(delays),
                     p95_ms=float(np.percentile(delays, 95)), max_ms=max(delays),
                     negative_delays=sum(value < 0 for value in delays),
                     max_interval_ms=max(gaps) if gaps else None,
                     timestamp_reversals=sum(value < 0 for value in gaps),
                     duplicate_timestamps=sum(value == 0 for value in gaps))
    return result


def write_timing(folder, report):
    (folder/'latency.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    embedded = json.dumps(report, ensure_ascii=False, allow_nan=False).replace('<', r'\u003c')
    page = '''<!doctype html><html lang="ja"><meta charset="utf-8"><title>Recorder遅延の確認</title>
<style>body{font:16px sans-serif;margin:24px;color:#222;background:#fafafa}canvas{width:100%;max-width:1200px;height:300px;background:white;border:1px solid #ddd}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #ccc}select{max-width:100%}a{color:#1764b4}</style>
<h1>取得元時刻 → Recorder受信の遅延</h1><a href="review.html">映像・関節・指令を見る</a><p id="episode"></p>
<p>カメラはホスト取得時刻、関節状態は読み取り完了時刻からRecorder受信までを表示します。露光からの遅延、操作画面での表示遅延、サーボの物理応答時間は含みません。</p>
<p>横軸：エピソード開始からの秒数。縦軸：ms。赤線は選択時刻。グラフをクリックすると下のスライダーも移動します。</p>
<label>トピック <select id="topic"></select></label><p id="summary"></p>
<canvas id="plot" width="1200" height="300"></canvas><br>
<label>記録時刻 <input id="time" type="range" min="0" step="0.001" value="0" style="width:80%"></label><p id="sample"></p>
<p>フレーム間隔は隣接する取得元時刻の差です。遅延とは別の値です。負の遅延や時刻逆転は、時計・時刻の定義を確認する手掛かりになります。</p>
<table><thead><tr><th>トピック</th><th>件数</th><th>中央値 ms</th><th>P95 ms</th><th>最大 ms</th><th>最大間隔 ms</th></tr></thead><tbody id="stats"></tbody></table>
<script id="data" type="application/json">__DATA__</script><script>
const d=JSON.parse(document.getElementById('data').textContent),q=id=>document.getElementById(id),fmt=v=>v==null?'—':v.toFixed(2);
q('episode').textContent=d.episode_id;
for(const [name,t]of Object.entries(d.topics)){const o=document.createElement('option');o.textContent=name;q('topic').append(o);const tr=document.createElement('tr');for(const v of[name,t.messages,fmt(t.median_ms),fmt(t.p95_ms),fmt(t.max_ms),fmt(t.max_interval_ms)]){const td=document.createElement('td');td.textContent=v;tr.append(td)}q('stats').append(tr)}
if(!d.index_available)q('summary').textContent='時刻索引がないため、取得から受信までの遅延は計算できません。';
let duration=1;for(const t of Object.values(d.topics))for(const s of t.samples)duration=Math.max(duration,s[0]);q('time').max=duration;
function draw(){const t=d.topics[q('topic').value];if(!t)return;const c=q('plot'),x=c.getContext('2d'),left=65,right=c.width-20,top=20,bottom=c.height-40;
const lo=t.samples.reduce((a,s)=>Math.min(a,s[1]),0),hi=t.samples.reduce((a,s)=>Math.max(a,s[1]),1),span=hi-lo||1,px=v=>left+v/duration*(right-left),py=v=>bottom-(v-lo)/span*(bottom-top);
x.clearRect(0,0,c.width,c.height);x.font='13px sans-serif';x.fillStyle='#444';x.strokeStyle='#ddd';
for(let i=0;i<=5;i++){const v=lo+span*i/5,y=py(v);x.beginPath();x.moveTo(left,y);x.lineTo(right,y);x.stroke();x.fillText(v.toFixed(1),4,y+4);x.fillText((duration*i/5).toFixed(2),px(duration*i/5)-12,bottom+24)}
x.strokeStyle='#1764b4';x.beginPath();t.samples.forEach((s,i)=>{if(i)x.lineTo(px(s[0]),py(s[1]));else x.moveTo(px(s[0]),py(s[1]))});x.stroke();const at=+q('time').value;x.strokeStyle='#c00';x.beginPath();x.moveTo(px(at),top);x.lineTo(px(at),bottom);x.stroke();
let best=0;t.samples.forEach((s,i)=>{if(Math.abs(s[0]-at)<Math.abs(t.samples[best][0]-at))best=i});const s=t.samples[best],gap=best?(s[0]-t.samples[best-1][0])*1000:null;
q('sample').textContent='選択付近 '+s[0].toFixed(3)+' s / 受信遅延 '+fmt(s[1])+' ms / 直前からの間隔 '+fmt(gap)+' ms';
q('summary').textContent='中央値 '+fmt(t.median_ms)+' ms / P95 '+fmt(t.p95_ms)+' ms / 最大 '+fmt(t.max_ms)+' ms / 負の遅延 '+t.negative_delays+' 件 / 時刻逆転 '+t.timestamp_reversals+' 件 / 重複 '+t.duplicate_timestamps+' 件';}
q('topic').onchange=draw;q('time').oninput=draw;q('plot').onclick=e=>{const r=e.currentTarget.getBoundingClientRect();q('time').value=Math.max(0,Math.min(duration,((e.clientX-r.left)*1200/r.width-65)/(1200-85)*duration));draw()};draw();
</script></html>'''
    (folder/'latency.html').write_text(page.replace('__DATA__', embedded), encoding='utf-8')


def _available_memory_bytes():
    """Respect both host availability and a container's cgroup memory limit."""
    available = None
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                available = int(line.split()[1])*1024
                break
    except (OSError, ValueError):
        pass
    for limit_path, used_path in (
        ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory.current'),
        ('/sys/fs/cgroup/memory/memory.limit_in_bytes', '/sys/fs/cgroup/memory/memory.usage_in_bytes')):
        try:
            limit = Path(limit_path).read_text().strip()
            if limit != 'max':
                remaining = max(0, int(limit)-int(Path(used_path).read_text()))
                available = remaining if available is None else min(available, remaining)
        except (OSError, ValueError):
            pass
    return available


def _available_cpus():
    count = os.cpu_count() or 1
    try:
        count = min(count, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    try:
        quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()
        if quota != 'max':
            count = min(count, max(1, math.ceil(int(quota)/int(period))))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    return max(1, count)


def _worker_count(requested, total, config, recordings):
    if type(requested) is not int or requested < 0:
        raise ValueError('workers must be a nonnegative integer (0 selects automatically)')
    if not total:
        raise ValueError('No episodes selected')
    if requested:
        return min(requested, total)
    count = min(4, _available_cpus(), total)
    available = _available_memory_bytes()
    if available is not None:
        budget = max(1., float(config.get('dataset', {}).get('max_memory_gb', 8.)))*1e9
        count = min(count, max(1, int(available*.75/budget)))
    # Raw recording import opens NVENC encoders per camera. Do not multiply
    # hardware encoder sessions automatically; explicit --workers can opt in.
    if recordings and not config.get('camera', {}).get('video', {}).get('enabled', False):
        count = 1
    return max(1, count)


def _worker_init():
    # The parent owns cancellation and waits for active episodes before deleting
    # staging files, so no child can keep writing into a removed output tree.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _render_episode(number, entry, staging, model, loader, cameras):
    folder = Path(staging)/'episodes'/f'{number:06d}'
    began = time.monotonic()
    folder.mkdir(parents=True)
    try:
        data, metadata, timing = loader(entry, folder)
        data['videos'] = {name: data['videos'][name] for name in cameras if name in data['videos']}
        if timing:
            start = timing.get('start_time_ns', metadata.get('start_time_ns'))
            if start is None and metadata.get('start_time') is not None:
                start = round(metadata['start_time']*1e9)
            timing['origin_known'] = start is not None
            timing['origin_offset_s'] = (int(start)-int(data.get('origin_timestamp_ns', data['rows'][0]['timestamp_ns'])))/1e9 if start is not None else 0.
            for name, video in data['videos'].items():
                source = timing.get('topics', {}).get('/camera/'+name+'/video', {})
                samples = source.get('samples', [])
                stats = video.setdefault('stats', {})
                if len(samples) > 1:
                    stats.setdefault('fps', (len(samples)-1)/(samples[-1][0]-samples[0][0]))
                    stats.setdefault('max_interval_ms', source.get('max_interval_ms'))
        enrich(data, model, metadata, timing)
        write_episode_data(folder, data)
        write_timing(folder, timing or {'episode_id':data['episode'], 'topics':{}, 'index_available':False})
        item = summary(data, entry.get('source'), number)
        item['episode_id'] = item['id']
        return number, item, None, time.monotonic()-began
    except (ValueError, OSError, KeyError) as error:
        shutil.rmtree(folder, ignore_errors=True)
        skipped = {'source': str(entry.get('source', entry.get('episode_id', ''))), 'reason': str(error)}
        return number, None, skipped, time.monotonic()-began


def _bundle(entries, output, config, model, loader, cameras, title, workers=0, recordings=False):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Output exists; choose a new --output directory: '+str(output))
    entries = list(entries)
    workers = _worker_count(workers, len(entries), config, recordings)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=output.name+'.partial-', dir=output.parent))
    results, timings = {}, {}
    began = time.monotonic()
    print(f'生成: {len(entries)} Episode / {workers} プロセス', flush=True)
    def started(number, entry):
        print(f"開始 [{number+1}/{len(entries)}]: "+str(entry.get('source', entry.get('episode_id', number))), flush=True)
    def completed(result):
        number, item, skipped, elapsed = result
        results[number] = (item, skipped)
        timings[number] = elapsed
        label = '完了' if item is not None else '未生成'
        detail = item['id'] if item is not None else skipped['reason']
        print(f'{label} [{len(results)}/{len(entries)}] ({elapsed:.1f}秒): '+str(detail), flush=True)
    try:
        if workers == 1:
            for number, entry in enumerate(entries):
                started(number, entry)
                completed(_render_episode(number, entry, staging, model, loader, cameras))
        else:
            # Spawn isolates ROS bag readers, codec contexts and Python's GIL.
            # Only `workers` jobs are submitted at a time to bound metadata and
            # IPC queues; full row/video payloads never return to the parent.
            pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn'),
                                       initializer=_worker_init)
            pending = {}
            remaining = iter(enumerate(entries))
            def submit():
                try:
                    number, entry = next(remaining)
                except StopIteration:
                    return
                started(number, entry)
                future = pool.submit(_render_episode, number, entry, staging, model, loader, cameras)
                pending[future] = number
            try:
                for _ in range(workers):
                    submit()
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        pending.pop(future)
                        completed(future.result())
                        submit()
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        # Completion order must not change episode IDs, folder names, initial
        # selection, cohort statistics or the order of skipped records.
        items = [results[n][0] for n in sorted(results) if results[n][0] is not None]
        skipped = [results[n][1] for n in sorted(results) if results[n][1] is not None]
        if not items:
            raise ValueError('No reviewable episodes. '+('; '.join(s['reason'] for s in skipped)))
        first = read_review_data(staging/items[0]['review'])
        workspace = write_workspace(staging, items, model, title, first)
        workspace['skipped'] = skipped
        from .review import write_page
        write_page(staging, workspace, first)
        (staging/'workspace.json').write_text(json.dumps(workspace, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        manifest_items = [{k:v for k,v in item.items() if k not in ('trajectory','normalized_ee')} for item in items]
        (staging/'review_manifest.json').write_text(json.dumps({'format': 'so101_recording_review_v2',
            'cameras': cameras, 'kinematics': workspace['kinematics'], 'episodes': manifest_items,
            'skipped': skipped}, ensure_ascii=False, indent=2), encoding='utf-8')
        elapsed = time.monotonic()-began
        (staging/'generation.json').write_text(json.dumps({'workers':workers, 'elapsed_s':elapsed,
            'episodes':len(entries), 'completed':len(items), 'skipped':len(skipped),
            'episode_elapsed_s':[timings[n] for n in sorted(timings)]}, indent=2)+'\n', encoding='utf-8')
        staging.rename(output)
        print(f'生成完了: {len(items)}/{len(entries)} Episode、{elapsed:.1f}秒', flush=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return output/'index.html'


def _load_recording(entry, folder, config):
    source = Path(entry['source'])
    metadata, states, actions, images, videos, _, _, operator = read_episode(
        source, config, folder/'videos', include_failed=True)
    required = list(config['dataset'].get('input_topics', {})) if metadata.get('control_mode_at_start') == 'TELEOP' else []
    rows = list(aligned_samples(states, actions, images, config['dataset']['fps'], alignment_settings(config['dataset']), operator, required))
    if not rows:
        raise ValueError('No common timeline for video/state/action: '+str(source))
    data = review_data(folder, metadata, rows, videos, config['robot']['joints'])
    return data, metadata, timing_report(source)


def generate(folders, output, config, cameras=None, model=None, workers=0):
    config = copy.deepcopy(config)
    names = cameras or list(config['camera']['cameras'])
    if not names or len(names) != len(set(names)) or any(n not in config['camera']['cameras'] for n in names):
        raise ValueError('Specify distinct configured camera names')
    config['observation']['cameras'] = names
    model = model or load_review_kinematics(config)
    loader = partial(_load_recording, config=config)
    return _bundle([{'source':str(p)} for p in folders], output, config, model, loader, names, 'Recording Review', workers, recordings=True)


def generate_existing(source, output, config, cameras=None, accepted_only=False, model=None, workers=0):
    source = Path(source).resolve()
    if (source/'review_manifest.json').is_file():
        manifest = json.loads((source/'review_manifest.json').read_text())
        if manifest.get('format') not in ('so101_recording_review_v1', 'so101_recording_review_v2'):
            raise ValueError('Unsupported review bundle')
        entries = manifest['episodes']
        if accepted_only:
            entries = [i for i in entries if i.get('accepted') is True]
        names = manifest['cameras']
        loader = partial(read_existing_episode, source)
    else:
        manifest = json.loads((source/'dataset.json').read_text())
        if manifest.get('format') != 'so101_dataset_v2':
            raise ValueError('Review currently supports Dataset v2 (MP4 + NPZ)')
        entries = manifest['episodes']
        names = list(manifest['image_shapes'])
        entries = [{**entry, 'source':str(source/f"episodes/{entry['index']:06d}")} for entry in entries]
        if accepted_only:
            def accepted(entry):
                path = Path(entry['source'])/'source_episode.json'
                saved = json.loads(path.read_text()) if path.exists() else {}
                return saved.get('episode', {}).get('collection_selection', {}).get('accepted') is True
            entries = [entry for entry in entries if accepted(entry)]
        loader = partial(read_dataset_episode, source)
    if not entries:
        raise ValueError('No episodes selected')
    if cameras and list(cameras) != list(names):
        raise ValueError('Existing review/Dataset camera selection is fixed. Use the original recordings with --cameras to recompute alignment.')
    return _bundle(entries, output, config, model or load_review_kinematics(config), loader, names, source.name, workers)


def latest_source():
    root = PROJECT/'data/collections'
    sessions = sorted(root.glob('*/session.json'), key=lambda p:p.stat().st_mtime, reverse=True)
    for path in sessions:
        data = json.loads(path.read_text())
        if data.get('status') not in ('starting','running','recording') and data.get('attempts') and all(Path(a['path']).is_dir() for a in data['attempts']):
            return path.parent
    raise ValueError('No completed collection with its source recordings was found. Specify a recording, existing review, Dataset, or bags directory.')


def main():
    parser = argparse.ArgumentParser(description='同期マルチカメラ再生・EE 3D軌跡・全Episode比較のローカル確認画面を生成')
    parser.add_argument('source', nargs='?', help='Episode / Session / bagsディレクトリー / 既存レビュー / Dataset v2。省略時は最新の終了済みSession')
    parser.add_argument('--output', help='出力先。省略時はdata/reviews内に新規作成')
    parser.add_argument('--config', help='収集時と一致するSystem YAML')
    parser.add_argument('--cameras', nargs='+', help='元記録から確認するカメラ。既定は全台')
    parser.add_argument('--workers', type=int, default=0, metavar='N', help='Episode並列数。0=CPU・空きメモリーから自動選択（最大4）、1=順次処理')
    parser.add_argument('--accepted-only', action='store_true', help='採用Episodeだけを確認')
    parser.add_argument('--urdf', help='EE計算用のURDFまたはXacro。省略時はリポジトリーの近似モデル')
    parser.add_argument('--base-frame', default='base_link')
    parser.add_argument('--ee-frame', default='gripper_link')
    parser.add_argument('--tcp-xyz', nargs=3, type=float, default=(0,0,0), metavar=('X','Y','Z'), help='EEフレーム内のTCPオフセット [m]')
    args = parser.parse_args()
    if args.workers < 0:
        parser.error('--workers must be 0 or a positive integer')
    try:
        source = Path(args.source).expanduser().resolve() if args.source else latest_source()
        existing = source.is_dir() and ((source/'review_manifest.json').is_file() or (source/'dataset.json').is_file())
        if existing:
            folders, hint, name = [], None, source.name
        elif source.is_dir() and not (source/'episode.json').is_file() and not (source/'session.json').is_file():
            folders = sorted(p for p in source.iterdir() if p.is_dir() and (p/'episode.json').is_file())
            if args.accepted_only:
                folders = [p for p in folders if (p/'collection.json').exists() and json.loads((p/'collection.json').read_text()).get('accepted') is True]
            if not folders:
                raise ValueError('No recorded episodes found under '+str(source))
            hint, name = None, source.name
        else:
            folders, hint, name = sources(source, args.accepted_only)
        config = load_system(args.config or hint or PROJECT/'config/runtime/system.real30hz.640x480.h264.yaml')
        model = load_review_kinematics(config, args.urdf, args.base_frame, args.ee_frame, args.tcp_xyz)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        output = Path(args.output) if args.output else PROJECT/'data/reviews'/f'{name}_{stamp}_{uuid.uuid4().hex[:6]}'
        print('入力: '+str(source), flush=True)
        index = generate_existing(source, output, config, args.cameras, args.accepted_only, model, args.workers) if existing else generate(folders, output, config, args.cameras, model, args.workers)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, str(error)+'\n')
    print('確認画面: '+str(index)+'\nブラウザでindex.htmlを開いてください。別のPCでは出力フォルダー全体をコピーしてください。', flush=True)


def _interrupt(signum, frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        main()
    except KeyboardInterrupt:
        print('生成を中断しました。実行中の処理を終了し、一時出力を片付けました。', file=sys.stderr)
        raise SystemExit(130)

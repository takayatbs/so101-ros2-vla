import gzip
import json
import multiprocessing
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import numpy as np
import pytest

from so101_core import recording_review
from so101_core.synchronization import Sample, Timeline


def episode(tmp_path):
    folder = tmp_path/'episode'
    (folder/'raw').mkdir(parents=True)
    (folder/'episode.json').write_text(json.dumps({'episode_id': 'example', 'start_time': 1.,
        'end_time': 2., 'start_time_ns': 1_000_000_000, 'end_time_ns': 2_000_000_000}))
    return folder


def test_timing_excludes_preroll_and_keeps_negative_delays(tmp_path):
    folder = episode(tmp_path)
    with gzip.open(folder/'raw/message_index.jsonl.gz', 'wt') as f:
        for source, delay in [(900_000_000, 999_000_000), (1_000_000_000, 10_000_000),
                              (1_100_000_000, 20_000_000), (1_200_000_000, -1_000_000),
                              (2_100_000_000, 999_000_000)]:
            f.write(json.dumps({'topic_name': '/camera/top/video', 'source_timestamp_ns': source,
                                'receive_timestamp_ns': source+delay})+'\n')
    report = recording_review.timing_report(folder)
    topic = report['topics']['/camera/top/video']
    assert topic['messages'] == 3
    assert topic['median_ms'] == 10
    assert topic['p95_ms'] == pytest.approx(19)
    assert topic['negative_delays'] == 1
    assert topic['max_interval_ms'] == pytest.approx(100)
    recording_review.write_timing(folder, report)
    assert json.loads((folder/'latency.json').read_text()) == report


def test_missing_and_plain_indices(tmp_path):
    folder = episode(tmp_path)
    assert not recording_review.timing_report(folder)['index_available']
    (folder/'raw/message_index.jsonl').write_text(json.dumps({'topic_name': '/state',
        'source_timestamp_ns': 1_000_000_000, 'receive_timestamp_ns': 1_001_000_000})+'\n')
    topic = recording_review.timing_report(folder)['topics']['/state']
    assert topic['median_ms'] == 1
    assert topic['max_interval_ms'] is None


def test_source_selection_includes_failed_attempts_and_mcap(tmp_path):
    folder = episode(tmp_path)
    mcap = folder/'raw/raw_0.mcap'
    mcap.touch()
    assert recording_review.sources(mcap)[0] == [folder]
    session = tmp_path/'session.json'
    data = {'format': 'so101_collection_v1', 'status': 'complete', 'runtime_config': 'recorded.yaml',
            'attempts': [{'path': str(folder)}, {'path': str(tmp_path/'failed')}],
            'accepted_episodes': [str(folder)]}
    session.write_text(json.dumps(data))
    assert len(recording_review.sources(session)[0]) == 2
    assert recording_review.sources(session, True)[0] == [folder]
    data['status'] = 'running'
    session.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Finish collection'):
        recording_review.sources(session)


def test_generate_reviews_all_invalid_rows_without_training_dataset(monkeypatch, tmp_path):
    source = episode(tmp_path)
    config = {'robot': {'joints': ['gripper']}, 'camera': {'cameras': {'top': {}}},
              'observation': {'cameras': ['top']}, 'dataset': {'fps': 30}}
    def read(folder, cfg, video_folder, include_failed):
        assert include_failed
        video_folder.mkdir(parents=True)
        np.savez(video_folder/'top.npz', pts_ns=[0, 100_000_000])
        (video_folder/'top.mp4').write_bytes(b'placeholder')
        sample = Sample(1., np.array([0.]), 1_000_000_000)
        timeline = Timeline([sample, Sample(1.1, np.array([1.]), 1_100_000_000)])
        metadata = {'episode_id': '</script>example', 'task_id': 'test', 'success': False}
        return metadata, timeline, timeline, {}, {'top': {'path': 'top.mp4', 'index_path': 'top.npz', 'timestamp_origin_ns': 1_000_000_000}}, {}, {}, {}
    monkeypatch.setattr(recording_review, 'read_episode', read)
    monkeypatch.setattr(recording_review, 'aligned_samples', lambda *a: [
        {'target_ns': 1_000_000_000+i*100_000_000, 'valid': False, 'warnings': [], 'reasons': ['gap'],
         'state': Sample(1., np.array([i]), 1_000_000_000), 'action': None,
         'errors': {'state': 0.}, 'images': {'top': None}} for i in range(2)])
    output = tmp_path/'output'
    assert recording_review.generate([source], output, config) == output/'index.html'
    assert not (output/'dataset.json').exists()
    manifest = json.loads((output/'review_manifest.json').read_text())
    assert manifest['episodes'][0]['valid_frames'] == 0
    page = (output/'episodes/000000/review.html').read_text()
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.S).group(1))
    assert data['episode'] == '</script>example'
    with pytest.raises(ValueError, match='Output exists'):
        recording_review.generate([source], output, config)


def test_failed_generation_removes_partial_output(monkeypatch, tmp_path):
    source = episode(tmp_path)
    config = {'camera': {'cameras': {'top': {}}}, 'observation': {}}
    def fail(*a, **kw):
        raise ValueError('damaged recording')
    monkeypatch.setattr(recording_review, 'read_episode', fail)
    with pytest.raises(ValueError, match='damaged recording'):
        recording_review.generate([source], tmp_path/'output', config)
    assert not (tmp_path/'output').exists()
    assert not list(tmp_path.glob('output.partial-*'))


def parallel_fixture_loader(entry, folder):
    """Picklable worker fixture; no ROS, codec or hardware dependency."""
    time.sleep(entry.get('delay', 0))
    if entry.get('fatal'):
        raise RuntimeError('unexpected worker failure')
    if entry.get('skip'):
        raise ValueError('damaged fixture')
    (folder/'worker_pid.txt').write_text(str(os.getpid()))
    if entry.get('release'):
        Path(entry['release']).touch()
    if entry.get('wait_for'):
        deadline = time.monotonic()+10
        while not Path(entry['wait_for']).exists():
            if time.monotonic() > deadline:
                raise RuntimeError('Second worker did not start')
            time.sleep(.01)
    data = {'episode':entry['source'], 'task':'fixture', 'joints':['slide'], 'videos':{},
            'origin_timestamp_ns':'0', 'rows':[
                {'t':float(i), 'timestamp_ns':str(i*1_000_000_000), 'valid':i != 1,
                 'warnings':[], 'reasons':['gap'] if i == 1 else [],
                 'state':[float(i)], 'action':[float(i)+.1], 'errors':{}} for i in range(3)]}
    return data, {'success':True}, None


def test_parallel_bundle_preserves_order_skips_initial_episode_and_all_review_bytes(tmp_path):
    entries = [{'source':'excluded-first', 'skip':True}, {'source':'slow-first'},
               {'source':'fast-second'}, {'source':'excluded-last', 'skip':True}]
    outputs = [tmp_path/'serial', tmp_path/'parallel']
    for workers, output in zip((1, 2), outputs):
        jobs = [dict(e) for e in entries]
        if workers > 1:
            jobs[1]['wait_for'] = str(tmp_path/'parallel-gate')
            jobs[2]['release'] = str(tmp_path/'parallel-gate')
        recording_review._bundle(jobs, output, {}, {'available':False}, parallel_fixture_loader, [], 'Fixture', workers)
    manifest = json.loads((outputs[1]/'review_manifest.json').read_text())
    assert [e['id'] for e in manifest['episodes']] == ['slow-first', 'fast-second']
    assert [e['source'] for e in manifest['skipped']] == ['excluded-first', 'excluded-last']
    initial = recording_review.read_review_data(outputs[1]/'index.html')
    assert initial['episode'] == 'slow-first'
    assert len({p.read_text() for p in outputs[1].glob('episodes/*/worker_pid.txt')}) == 2
    files = {str(p.relative_to(outputs[0])) for p in outputs[0].rglob('*') if p.is_file()}
    assert files == {str(p.relative_to(outputs[1])) for p in outputs[1].rglob('*') if p.is_file()}
    for name in files:
        if Path(name).name not in ('generation.json', 'worker_pid.txt'):
            assert (outputs[0]/name).read_bytes() == (outputs[1]/name).read_bytes(), name


def test_parallel_failure_waits_for_children_and_removes_partial_output(tmp_path):
    before = {p.pid for p in multiprocessing.active_children()}
    output = tmp_path/'failed'
    with pytest.raises(RuntimeError, match='unexpected worker failure'):
        recording_review._bundle([{'source':'broken', 'fatal':True}, {'source':'busy', 'delay':.1}],
            output, {}, {'available':False}, parallel_fixture_loader, [], 'Fixture', workers=2)
    assert not output.exists()
    assert not list(tmp_path.glob('failed.partial-*'))
    assert not ({p.pid for p in multiprocessing.active_children()}-before)


def test_automatic_workers_respect_cpu_memory_raw_encoding_and_explicit_override(monkeypatch):
    monkeypatch.setattr(recording_review, '_available_cpus', lambda: 12)
    monkeypatch.setattr(recording_review, '_available_memory_bytes', lambda: 64e9)
    config = {'camera':{'video':{'enabled':True}}, 'dataset':{'max_memory_gb':8}}
    assert recording_review._worker_count(0, 10, config, True) == 4
    assert recording_review._worker_count(0, 2, config, True) == 2
    monkeypatch.setattr(recording_review, '_available_memory_bytes', lambda: 16e9)
    assert recording_review._worker_count(0, 10, config, True) == 1
    config['camera']['video']['enabled'] = False
    monkeypatch.setattr(recording_review, '_available_memory_bytes', lambda: 64e9)
    assert recording_review._worker_count(0, 10, config, True) == 1
    assert recording_review._worker_count(0, 10, config, False) == 4
    assert recording_review._worker_count(8, 10, config, True) == 8
    assert recording_review._worker_count(8, 2, config, True) == 2
    for value in (-1, True, 1.5):
        with pytest.raises(ValueError, match='workers'):
            recording_review._worker_count(value, 10, config, True)


def test_cli_rejects_negative_workers_before_reading_inputs(monkeypatch):
    monkeypatch.setattr('sys.argv', ['recording_review', '--workers', '-1'])
    with pytest.raises(SystemExit) as error:
        recording_review.main()
    assert error.value.code == 2


def test_parent_sigterm_cleans_staging_and_joins_active_workers(tmp_path):
    bootstrap = tmp_path/'interrupt.py'
    bootstrap.write_text('''import json,multiprocessing,signal,sys
from pathlib import Path
from so101_core import recording_review
from test_recording_review import parallel_fixture_loader

if __name__ == '__main__':
    signal.signal(signal.SIGTERM, recording_review._interrupt)
    root=Path(sys.argv[1])
    entries=[{'source':str(i),'wait_for':str(root/'release')} for i in range(3)]
    try:
        recording_review._bundle(entries,root/'output',{}, {'available':False},parallel_fixture_loader,[],'Interrupt',workers=2)
    except KeyboardInterrupt:
        assert not multiprocessing.active_children()
        assert not list(root.glob('output.partial-*'))
        (root/'interrupted').touch()
    else:
        raise RuntimeError('Expected cancellation')
''')
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root/'tests'), str(root/'ros2_ws/src/so101_core'), os.environ.get('PYTHONPATH', '')]))
    proc = subprocess.Popen([sys.executable, str(bootstrap), str(tmp_path)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic()+15
        while len(list(tmp_path.glob('output.partial-*/episodes/*/worker_pid.txt'))) < 2:
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.fail('Workers did not become active')
            time.sleep(.02)
        proc.send_signal(signal.SIGTERM)
        (tmp_path/'release').touch()
        log, _ = proc.communicate(timeout=15)
        assert proc.returncode == 0, log
        assert (tmp_path/'interrupted').exists()
        assert not (tmp_path/'output').exists()
    finally:
        (tmp_path/'release').touch()
        if proc.poll() is None:
            proc.terminate()
            proc.communicate(timeout=15)

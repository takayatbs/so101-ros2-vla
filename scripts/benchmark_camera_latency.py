#!/usr/bin/env python3
"""Compare camera pipelines with actual USB/NVENC/MCAP, without starting robot drivers.

Run inside the project container under data/.compute.lock. Each camera runs in
its own ROS process, as in Runtime. Raw benchmark bags deliberately have no
episode.json and cannot be mistaken for training demonstrations.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import numpy as np
import rclpy
import rosbag2_py
from rclpy.executors import SingleThreadedExecutor
from rclpy.serialization import deserialize_message
from diagnostic_msgs.msg import DiagnosticArray
from std_msgs.msg import String
from so101_core.config import load_system, resolve
from so101_dataset.recorder import RecorderNode
from so101_msgs.srv import StartRecording


def stats(values):
    return dict(zip(('min', 'p50', 'p95', 'max'), map(float, np.percentile(values, [0, 50, 95, 100])))) if len(values) else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/workspace/config/runtime/system.real30hz.640x480.h264.yaml')
    parser.add_argument('--duration', type=float, default=20.)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.duration < 3:
        parser.error('Use at least 3 seconds')
    if args.output.exists():
        parser.error('Output already exists')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Isolate these camera-only processes from any interactive ROS clients.
    os.environ['ROS_DOMAIN_ID'] = str(100+os.getpid()%100)
    config = load_system(args.config)
    names = list(config['camera']['cameras'])
    from so101_hardware import camera
    source_hash = hashlib.sha256(Path(camera.__file__).read_bytes()).hexdigest()
    rclpy.init(args=['--ros-args', '-p', 'config:='+args.config])
    recorder = RecorderNode()
    peer = rclpy.create_node('camera_latency_probe')
    executor = SingleThreadedExecutor()
    executor.add_node(recorder); executor.add_node(peer)
    metadata, diagnostics = {name: {} for name in names}, []
    for name in names:
        def frame(msg, name=name):
            item = json.loads(msg.data)
            metadata[name][item['timestamp_ns']] = item
        peer.create_subscription(String, f'/camera/{name}/frame_metadata', frame, 30)
    peer.create_subscription(DiagnosticArray, '/diagnostics',
                             lambda msg: diagnostics.extend((s.name, s.message,
                                 int.from_bytes(s.level, 'little') if isinstance(s.level, bytes) else int(s.level))
                                 for s in msg.status), 100)
    processes, logs = [], []
    bag = resolve(config['dataset'], config['dataset']['bag_root'])/('camera_latency_probe_'+uuid.uuid4().hex[:10])
    start_ns = end_ns = None
    try:
        for name in names:
            log = args.output.with_name(args.output.stem+'_'+name+'.log').open('w')
            logs.append(log)
            processes.append(subprocess.Popen([sys.executable, '-c', 'from so101_hardware.camera import main; main()',
                '--ros-args', '-r', '__node:=camera_'+name, '-p', 'camera:='+name, '-p', 'backend:=real',
                '-p', 'config:='+args.config], stdout=log, stderr=subprocess.STDOUT))
        deadline = time.monotonic()+40
        while not all(len(v) >= 20 for v in metadata.values()):
            executor.spin_once(timeout_sec=.01)
            if time.monotonic() > deadline or any(p.poll() is not None for p in processes):
                raise RuntimeError('Cameras failed readiness; inspect camera logs')
        response = recorder.start(StartRecording.Request(bag_uri=str(bag/'raw')), StartRecording.Response())
        if not response.success:
            raise RuntimeError(response.message)
        start_ns = peer.get_clock().now().nanoseconds
        deadline = time.monotonic()+args.duration
        while time.monotonic() < deadline:
            executor.spin_once(timeout_sec=.005)
            if any(p.poll() is not None for p in processes):
                raise RuntimeError('Camera exited during measurement')
        end_ns = peer.get_clock().now().nanoseconds
        recorder.close_recording()
    finally:
        for process in processes:
            if process.poll() is None: process.send_signal(signal.SIGINT)
        for process in processes:
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        recorder.destroy_node(); peer.destroy_node(); executor.shutdown(); rclpy.shutdown()
        for log in logs: log.close()
    topics = {}
    with gzip.open(bag/'raw/message_index.jsonl.gz', 'rt') as stream:
        for line in stream:
            item = json.loads(line)
            stamp = item['source_timestamp_ns']
            if stamp is not None and start_ns <= stamp <= end_ns:
                topics.setdefault(item['topic_name'], []).append(item)
    # The Recorder may process the last message before this probe's parallel
    # subscription does. Read authoritative frame metadata from the closed bag
    # rather than treating the probe's callback delivery as lossless.
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(bag/'raw'), storage_id='mcap'),
                rosbag2_py.ConverterOptions('cdr', 'cdr'))
    reader.set_filter(rosbag2_py.StorageFilter(topics=[f'/camera/{name}/frame_metadata' for name in names]))
    while reader.has_next():
        topic, data, _ = reader.read_next()
        item = json.loads(deserialize_message(data, String).data)
        metadata[topic.split('/')[2]][item['timestamp_ns']] = item
    result = {'method': 'Three real USB cameras, separate processes, shared NVENC, actual Recorder MCAP; no robot drivers',
              'camera_source_sha256': source_hash, 'duration_s': (end_ns-start_ns)/1e9,
              'bag': str(bag), 'ros_domain_id': os.environ['ROS_DOMAIN_ID'], 'cameras': {},
              'diagnostic_errors': [d for d in diagnostics if d[2] >= 2]}
    for name in names:
        video = topics.get(f'/camera/{name}/video', [])
        meta = topics.get(f'/camera/{name}/frame_metadata', [])
        if len(video) < 2 or len(meta) < 2:
            raise RuntimeError('Insufficient recorded camera samples: '+name)
        stamps = np.array([i['source_timestamp_ns'] for i in video], dtype=np.int64)
        delays = [(i['receive_timestamp_ns']-i['source_timestamp_ns'])/1e6 for i in video]
        intervals = np.diff(stamps)/1e6
        items = [metadata[name][i['source_timestamp_ns']] for i in meta]
        sequences = [i['source_sequence'] for i in items]
        phases = {}
        for field in ('capture_ready_timestamp_ns', 'metadata_generated_timestamp_ns', 'publish_started_timestamp_ns'):
            phases[field] = stats([(i[field]-i['timestamp_ns'])/1e6 for i in items if field in i])
        result['cameras'][name] = {'frames': len(video), 'actual_fps': (len(video)-1)*1e9/(stamps[-1]-stamps[0]),
            'receive_delay_ms': stats(delays), 'source_interval_ms': stats(intervals),
            'source_gaps_over_50ms': int((intervals > 50).sum()),
            'capture_sequence_skips': sum(max(0, b-a-1) for a,b in zip(sequences,sequences[1:])),
            'timestamp_reversals': int((intervals < 0).sum()), 'duplicate_timestamps': int((intervals == 0).sum()),
            'capture_to_stage_ms': phases}
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    main()

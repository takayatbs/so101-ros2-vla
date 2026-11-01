#!/usr/bin/env python3
"""One interactive session for standard/VLASH x smoothing, with compressed ROS recordings."""
import argparse
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid
from datetime import datetime, timezone

from so101_core.collection import TerminalKeys, atomic_json
from so101_core.config import load_config, load_system, resolve
from so101_core.benchmark import BenchmarkRunner

PROJECT = Path(__file__).resolve().parents[1]
module = importlib.util.spec_from_file_location('so101_comparison_cli', PROJECT/'scripts/compare_policies.py')
comparison = importlib.util.module_from_spec(module)
module.loader.exec_module(comparison)


def latest_model(method, model_root):
    models = []
    for manifest in model_root.glob('*/deployment.json'):
        data = comparison.read(manifest)
        if (data['training_config'].get('method') == method
                and data['training_config'].get('policy') == 'smolvla'):
            models.append(manifest)
    if not models:
        raise ValueError('No registered SmolVLA model for '+method)
    return max(models, key=lambda p: p.stat().st_mtime_ns).parent.name


class Journal:
    def __init__(self, folder, plan):
        self.folder, self.plan = folder, plan
        self.data = {'format': 'so101_interactive_benchmark_v1', 'status': 'starting',
                     'created_at': datetime.now(timezone.utc).isoformat(), 'trials': [],
                     'time_reference': 'AUTONOMOUS service request; acknowledgement also recorded',
                     'compression': 'camera H.264 in MCAP; message index gzip; reset periods not recorded'}
        self.events = (folder/'events.jsonl').open('a', buffering=1)
        self.save()

    def save(self):
        atomic_json(self.folder/'session.json', self.data)

    def phase(self, phase, trial, remaining, paused):
        self.data.update(phase=phase, current_trial=trial, remaining_s=remaining, paused=paused)
        self.save()

    def begin(self, trial):
        self.data['trials'].append({**trial, 'status': 'starting'})
        self.data['status'] = 'running'
        self.save()

    def started(self, trial, start):
        self.data['trials'][-1].update(start, status='running')
        self.save()

    def finish(self, trial, data):
        # A recovery sidecar is durable even if result validation or disk writing fails.
        atomic_json(self.folder/f'recovery-{trial["trial_id"]:04d}.json', {**trial, **data})
        comparison.record(self.folder, trial['trial_id'], data['outcome'],
                          {'duration_s': data['duration_s']}, data.get('reason', ''), data.get('recording'))
        self.data['trials'][-1].update(data, status='saved')
        self.save()
        recovery = self.folder/f'recovery-{trial["trial_id"]:04d}.json'
        recovery.unlink()

    def end(self, status, reason):
        self.data.update(status=status, reason=reason, ended_at=datetime.now(timezone.utc).isoformat())
        self.save()
        # Each session exports to a new directory exactly once, including partial sessions.
        if not (self.folder/'report').exists():
            comparison.summarize(self.folder, self.folder/'report')


def ros_types():
    # Delay ROS imports so --help also works without a running ROS graph.
    import rclpy
    from rclpy.signals import SignalHandlerOptions
    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
    from std_msgs.msg import String
    from std_srvs.srv import Trigger
    from so101_msgs.srv import SelectPolicy, SetControlMode, StartEpisode
    from so101_core.ros_utils import LATCHED_QOS
    from so101_dataset.collect import RosCollectionAdapter, RuntimeProcess
    return SimpleNamespace(**locals())


def adapter_classes(ros):
    class Runtime(ros.RuntimeProcess):
        def start(self):
            self.log = (self.folder/'runtime.log').open('w')
            # Match runtime.sh: a 640x480 RGB frame exceeds Fast DDS's default
            # shared-memory segment. H.264 recording can look healthy while the
            # separate raw-image inference subscriptions stop receiving frames.
            environment = dict(os.environ)
            environment['FASTRTPS_DEFAULT_PROFILES_FILE'] = (
                environment.get('FASTRTPS_DEFAULT_PROFILES_FILE') or str(PROJECT/'config/runtime/fastdds.xml'))
            self.proc = subprocess.Popen([sys.executable, str(PROJECT/'scripts/runtime_supervisor.py'),
                'run', uuid.uuid4().hex, 'ros2', 'launch', 'so101_bringup', 'runtime.launch.py',
                'backend:='+self.settings.backend, 'config:='+str(self.settings.runtime_config),
                'policy_backend:=lerobot', 'model_id:='+self.settings.models['standard'],
                'benchmark_models:='+json.dumps(list(self.settings.models.values())),
                'model_python:=/workspace/data/envs/policies/bin/python', 'asynchronous_inference:=true',
                'foxglove:=true'], stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True,
                env=environment)

    class Adapter(ros.RosCollectionAdapter):
        def __init__(self, settings, runtime, journal):
            super().__init__(settings, runtime)
            self.journal = journal
            self.condition, self.attempt = None, None
            self.clients['/policy/select'] = self.node.create_client(ros.SelectPolicy, '/policy/select')
            self.clients['/policy/check_ready'] = self.node.create_client(ros.Trigger, '/policy/check_ready')
            self.subscriptions.append(self.node.create_subscription(ros.String, '/policy/condition',
                lambda m: setattr(self, 'condition', json.loads(m.data)), ros.LATCHED_QOS))
            self.state_pub = self.node.create_publisher(ros.String, '/benchmark/state', ros.LATCHED_QOS)
            self.event_pub = self.node.create_publisher(ros.String, '/benchmark/events', 100)
            self.progress_state = None
            self.node.create_timer(1., self.publish_progress)

        @property
        def has_attempt(self):
            return self.attempt is not None

        def event(self, kind, **values):
            row = {'event': kind, 'timestamp_ns': self.node.get_clock().now().nanoseconds,
                   'monotonic_ns': time.monotonic_ns(), **values}
            self.journal.events.write(json.dumps(row, ensure_ascii=False)+'\n')
            self.event_pub.publish(ros.String(data=json.dumps(row, ensure_ascii=False)))
            return row

        def progress(self, phase, trial, remaining, paused):
            self.progress_state = {'phase': phase, 'trial': trial, 'remaining_s': remaining, 'paused': paused}
            self.publish_progress()

        def publish_progress(self):
            if self.progress_state is None:
                return
            row = {**self.progress_state, 'timestamp_ns': self.node.get_clock().now().nanoseconds}
            self.state_pub.publish(ros.String(data=json.dumps(row, ensure_ascii=False)))
            trial = row['trial'] or {}
            saved = [r for r in self.journal.data['trials'] if r.get('status') == 'saved']
            values = {'phase': row['phase'], 'trial': f'{trial.get("trial_id", 0)}/{len(self.journal.plan["trials"])}',
                      'condition': trial.get('condition', ''),
                      'model_id': self.settings.models.get(trial.get('method'), ''),
                      'smoothing': trial.get('smoothing', ''), 'remaining_s': f'{row["remaining_s"]:.1f}',
                      'paused': row['paused'], 'saved_trials': len(saved),
                      **{key: sum(r.get('outcome') == key for r in saved) for key in ('success', 'failure', 'aborted')},
                      'keys': '→/s: success | ←/f: failure | Space/p: pause | q/Esc: quit'}
            msg = ros.DiagnosticArray()
            msg.header.stamp = self.node.get_clock().now().to_msg()
            msg.status = [ros.DiagnosticStatus(name='benchmark_session', hardware_id='so101',
                level=ros.DiagnosticStatus.ERROR if row['phase'] == 'ERROR' else ros.DiagnosticStatus.OK,
                message='Paused' if row['paused'] else row['phase'],
                values=[ros.KeyValue(key=k, value=str(v)) for k,v in values.items()])]
            self.diag_pub.publish(msg)

        def check_health(self, expected='IDLE'):
            if self.mode != expected:
                raise RuntimeError('Benchmark stopped: control mode is '+str(self.mode))
            now = time.monotonic()
            if now-self.seen.get('follower', 0) > self.config['safety']['state_timeout_s']:
                raise RuntimeError('Stale follower state')
            for name in self.config['camera']['cameras']:
                if now-self.seen.get('camera_'+name, 0) > self.config['camera']['timeout_s']:
                    raise RuntimeError('Stale camera '+name)
            for name in ('data_recorder', 'so101_follower_driver', 'policy_node',
                         *('camera_'+n for n in self.config['camera']['cameras'])):
                if name == 'policy_node' and expected == 'IDLE':
                    # First camera/state arrivals can produce a transient pre-warm-up diagnostic.
                    # Selection waits for a generation-matched ready message below.
                    continue
                status = self.diagnostics.get(name)
                if status is not None and status.level >= ros.DiagnosticStatus.ERROR:
                    raise RuntimeError(name+': '+status.message)

        def select(self, trial):
            deadline = time.monotonic()+self.settings.startup_timeout_s
            self.wait_idle_health(deadline)
            request = ros.SelectPolicy.Request(model_id=self.settings.models[trial['method']],
                                                smoothing=trial['smoothing'])
            while True:
                self.pump()
                result = self.wait(self.clients['/policy/select'].call_async(request))
                if result.success:
                    break
                if not result.message.startswith('Inference busy') or time.monotonic() >= deadline:
                    raise RuntimeError(result.message)
                # Pump rather than sleeping while the old, command-free warm-up finishes.
                end = time.monotonic()+.1
                while time.monotonic() < end:
                    self.pump(.02)
            while time.monotonic() < deadline:
                self.pump(.02)
                self.wait_idle_health(deadline)
                c = self.condition
                if (c and c['generation'] == result.generation and c['model_id'] == request.model_id
                        and c['smoothing'] == request.smoothing and c['ready']):
                    self.diagnostics.pop('policy_node', None)
                    self.event('condition_ready', trial=trial, condition=c)
                    return
            raise TimeoutError('Selected model did not finish IDLE warm-up')

        def wait_idle_health(self, deadline):
            while time.monotonic() < deadline:
                self.pump(.02)
                try:
                    self.check_health()
                    return
                except RuntimeError as error:
                    # Before arming, wait for transient camera/state startup gaps.
                    # Device errors and FAULT/ESTOP are never retried here.
                    if not str(error).startswith(('Stale camera ', 'Stale follower state')):
                        raise
            raise TimeoutError('Fresh IDLE camera/state observations were not available')

        def check_policy_inputs(self, trial):
            deadline = time.monotonic()+self.settings.startup_timeout_s
            last = None
            while time.monotonic() < deadline:
                self.wait_idle_health(deadline)
                result = self.wait(self.clients['/policy/check_ready'].call_async(ros.Trigger.Request()))
                last = json.loads(result.message)
                if result.success:
                    if (last['model_id'] != self.settings.models[trial['method']]
                            or last['smoothing'] != trial['smoothing']
                            or last['generation'] != self.condition['generation']):
                        raise RuntimeError('Policy readiness belongs to another condition')
                    self.event('inference_inputs_checked', trial=trial, inputs=last)
                    return last
            raise TimeoutError('Policy inputs remained unavailable: '+str(last))

        def begin(self, trial):
            self.check_policy_inputs(trial)
            if self.episode is not None and self.episode.state == 'RECORDING':
                raise RuntimeError('Another episode is recording')
            self.claimed = True
            self.attempt = {'trial': trial, 'model_id': self.settings.models[trial['method']],
                            'condition_generation': self.condition['generation']}
            self.pending_start = self.clients['/start_episode'].call_async(ros.StartEpisode.Request(
                task_id=self.settings.task, operator_name=self.settings.operator))
            folder = self.resolve_start(self.wait(self.pending_start))
            self.attempt['recording'] = str(folder)
            self.event('recording_opened', trial=trial, recording=str(folder))
            # Opening a bag or waiting at READY must not age the camera input
            # unnoticed. Check the policy's raw input path before enabling torque.
            self.attempt['inference_inputs_at_start'] = self.check_policy_inputs(trial)
            self.attempt.update(start_monotonic_s=time.monotonic(),
                                start_timestamp_ns=self.node.get_clock().now().nanoseconds)
            atomic_json(folder/'benchmark.json', self.attempt)
            self.event('autonomous_requested', **self.attempt)
            self.call('/set_control_mode', ros.SetControlMode.Request(mode='AUTONOMOUS'))
            self.attempt['start_ack_timestamp_ns'] = self.node.get_clock().now().nanoseconds
            deadline = time.monotonic()+self.settings.service_timeout_s
            while self.mode != 'AUTONOMOUS' and time.monotonic() < deadline:
                self.pump(.01)
            self.check_health('AUTONOMOUS')
            atomic_json(folder/'benchmark.json', self.attempt)
            return dict(self.attempt)

        def finish(self, outcome, reason, ended):
            info = dict(self.attempt)
            errors = []
            duration = max(0., ended-info.get('start_monotonic_s', ended))
            info.update(outcome=outcome, reason=reason, duration_s=duration,
                        end_monotonic_s=ended, end_timestamp_ns=self.node.get_clock().now().nanoseconds)
            self.event('stop_requested', **info)
            # Disarm before flushing the bag or doing result I/O. Never reset FAULT/ESTOP here.
            try:
                if self.mode not in ('FAULT', 'ESTOP'):
                    self.call('/set_control_mode', ros.SetControlMode.Request(mode='IDLE'), check_process=False)
                    deadline = time.monotonic()+self.settings.service_timeout_s
                    while self.mode != 'IDLE' and time.monotonic() < deadline:
                        self.pump(.01, check_process=False)
                    if self.mode != 'IDLE':
                        raise RuntimeError('IDLE confirmation timed out')
                else:
                    errors.append('Runtime entered '+self.mode)
                info['stop_ack_timestamp_ns'] = self.node.get_clock().now().nanoseconds
            except Exception as error:
                errors.append('Stop failed: '+str(error))
                # Do not spend recording-flush time with an unconfirmed armed Runtime.
                self.runtime.stop()
            try:
                if self.pending_start is not None:
                    folder = self.resolve_start(self.wait(self.pending_start, check_process=False))
                    info['recording'] = str(folder)
                self.event('trial_end', **info)
                if self.active is not None:
                    folder = self.active
                    metadata = self.stop_episode(outcome == 'success' and not errors, reason, check_process=False)
                    info['episode_status'] = metadata['status']
                    if outcome == 'success' and not metadata['success']:
                        errors.append('Episode rejected success: '+metadata.get('failure_reason', ''))
                    summary = json.loads((folder/'recording_summary.json').read_text())
                    required = ['/hardware/follower/state',
                                *(f'/camera/{n}/video' for n in self.config['camera']['cameras'])]
                    missing = [n for n in required if summary.get(n, {}).get('messages', 0) == 0]
                    if missing:
                        errors.append('Recording missing required samples: '+', '.join(missing))
                    info['recorded_messages'] = {n: s['messages'] for n, s in summary.items()}
                else:
                    errors.append('No recording was opened')
            except Exception as error:
                errors.append('Recording close failed: '+str(error))
            if errors:
                info.update(outcome='aborted', error='; '.join(errors), reason=reason+'; '+'; '.join(errors))
            if info.get('recording'):
                atomic_json(Path(info['recording'])/'benchmark.json', info)
            self.attempt = None
            self.claimed = False
            return info

        def cleanup(self, reason):
            # The runner normally closes its own attempt. This covers an interrupted start request.
            if self.attempt is not None:
                return self.finish('aborted', reason, time.monotonic())
            if self.claimed:
                self.call('/set_control_mode', ros.SetControlMode.Request(mode='IDLE'), check_process=False)
    return Runtime, Adapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(PROJECT/'config/benchmark/benchmark.yaml'))
    parser.add_argument('--backend', choices=['real', 'mock', 'mujoco'])
    parser.add_argument('--standard-model')
    parser.add_argument('--vlash-model')
    parser.add_argument('--model-root', type=Path, default=PROJECT/'data/models')
    parser.add_argument('--trials-per-condition', type=int)
    parser.add_argument('--reset-time-s', type=float)
    parser.add_argument('--max-duration-s', type=float)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error('対話端末で起動してください。SSHはssh -tを使用してください。')
    config = load_config(args.config)
    model_root = args.model_root.resolve()
    models = {m: getattr(args, m+'_model') or config.get('models', {}).get(m) or latest_model(m, model_root)
              for m in ('standard', 'vlash')}
    for method, model_id in models.items():
        if comparison.registered(model_root, model_id, method) is None:
            parser.error('Model is not registered: '+model_id)
    repetitions = args.trials_per_condition if args.trials_per_condition is not None else config.get('trials_per_condition', 30)
    reset = args.reset_time_s if args.reset_time_s is not None else config.get('reset_time_s', 15.)
    limit = args.max_duration_s if args.max_duration_s is not None else config.get('max_duration_s', 30.)
    for name, value in [('reset_time_s', reset), ('max_duration_s', limit)]:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0 or (name == 'max_duration_s' and value == 0):
            parser.error(name+' must be finite and positive (reset may be zero)')
    first = model_root/models['standard']
    manifest = comparison.read(first/'deployment.json')
    runtime_config = (first/manifest['runtime_profiles']['none']).resolve()
    if not runtime_config.is_relative_to(first.resolve()):
        parser.error('Runtime profile leaves model folder')
    system = load_system(runtime_config)
    if not system['camera'].get('video', {}).get('enabled'):
        parser.error('Benchmark requires H.264 recording')
    expected_videos = {f'/camera/{n}/video' for n in system['camera']['cameras']}
    if not expected_videos.issubset(set(system['dataset']['topics'])) or any(t.endswith('/image_raw') for t in system['dataset']['topics']):
        parser.error('Record all cameras as compressed /video; raw image recording is disabled')
    identity = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]
    folder = resolve(config, config.get('output_root', '../../data/benchmarks')).resolve()/identity
    comparison.prepare(folder, models['standard'], models['vlash'], model_root, repetitions,
                       config.get('seed', 42), limit, config['success_rule'])
    settings = SimpleNamespace(runtime_config=runtime_config, backend=args.backend or config.get('backend', 'mock'),
        models=models, task=system['policy']['task'], operator=config.get('operator', 'evaluation'),
        startup_timeout_s=config.get('startup_timeout_s', 600.),
        service_timeout_s=config.get('service_timeout_s', 15.), start_runtime=True)
    ros = ros_types()
    Runtime, Adapter = adapter_classes(ros)
    with (PROJECT/'data/.collection.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another collection/benchmark session is running')
        journal = Journal(folder, comparison.read(folder/'plan.json'))
        journal.data.update(runtime_config=str(runtime_config), backend=settings.backend,
                            models=models, reset_time_s=reset)
        journal.save()
        runtime = Runtime(settings, folder)
        ros.rclpy.init(args=[], signal_handler_options=ros.SignalHandlerOptions.NO)
        adapter = Adapter(settings, runtime, journal)
        def stop_signal(signum, frame):
            raise KeyboardInterrupt('signal '+str(signum))
        old = {s: signal.signal(s, stop_signal) for s in (signal.SIGINT, signal.SIGTERM)}
        code = 0
        try:
            if adapter.clients['/set_control_mode'].wait_for_service(timeout_sec=.5):
                raise RuntimeError('Another Runtime is running; stop it before benchmarking')
            print('モデル:', json.dumps(models, ensure_ascii=False), '\nBackend:', settings.backend,
                  '\n成功条件:', config['success_rule'], '\n保存先:', folder, flush=True)
            print('Foxglove: ws://127.0.0.1:'+os.environ.get('SO101_FOXGLOVE_PORT', '8765')+
                  '\nLayout: config/foxglove/so101-benchmark-h264.json\n'
                  '操作用PCから接続する場合は8765のSSH転送が必要です。', flush=True)
            print('→/s: 成功  ←/f: 失敗  a: 中断  Space/p: 一時停止・再開  q/Esc: 終了\n'
                  '時間切れは失敗。戻し中はIDLE・トルクOFF。動作中の一時停止は中断として保存します。', flush=True)
            adapter.progress('STARTING', None, 0., False)
            runtime.start()
            with TerminalKeys(bindings={b' ': 'pause', b'p': 'pause', b'a': 'abort'},
                              arrows={b'C': 'save', b'D': 'fail'}) as keys:
                BenchmarkRunner(journal.plan['trials'], adapter, keys, journal, reset, limit,
                                output=lambda s: print(s, flush=True)).run()
        except KeyboardInterrupt:
            code = 130
            if journal.data['status'] == 'starting':
                journal.end('interrupted', 'Interrupted during startup')
        except Exception as error:
            print(str(error), file=sys.stderr, flush=True)
            code = 1
            if journal.data['status'] == 'starting':
                journal.end('error', str(error))
        finally:
            # Preserve ROS while disarming and flushing recordings, then terminate the owned Runtime.
            try:
                adapter.cleanup('Benchmark client exiting')
            finally:
                try:
                    runtime.stop()
                finally:
                    adapter.node.destroy_node()
                    if ros.rclpy.ok():
                        ros.rclpy.shutdown()
                    for signum, handler in old.items():
                        signal.signal(signum, handler)
                    journal.events.close()
            print('結果:', folder/'report/results.md', flush=True)
        raise SystemExit(code)


if __name__ == '__main__':
    main()

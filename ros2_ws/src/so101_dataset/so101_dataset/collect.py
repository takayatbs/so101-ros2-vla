"""LeRobot-style episode controls using the existing ROS Runtime services."""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import rclpy
from rclpy.signals import SignalHandlerOptions
from controller_manager_msgs.srv import ListControllers
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from rcl_interfaces.srv import GetParameters
from sensor_msgs.msg import JointState
from std_msgs.msg import String
from so101_msgs.msg import EpisodeState
from so101_msgs.srv import SetControlMode, StartEpisode, StopEpisode
from so101_core.collection import CollectionSettings, CollectionJournal, CollectionRunner, TerminalKeys
from so101_core.config import load_system, resolve, config_path
from so101_core.ros_utils import LATCHED_QOS, STATE_QOS


PROJECT = Path(__file__).resolve().parents[4]


class RuntimeProcess:
    def __init__(self, settings, folder):
        self.settings, self.folder = settings, folder
        self.proc, self.log = None, None

    def start(self):
        self.log = (self.folder/'runtime.log').open('w')
        self.proc = subprocess.Popen([sys.executable, str(PROJECT/'scripts/runtime_supervisor.py'),
            'run', uuid.uuid4().hex, 'ros2', 'launch', 'so101_bringup', 'runtime.launch.py',
            'backend:='+self.settings.backend, 'config:='+str(self.settings.runtime_config),
            'foxglove:=true'], stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)

    def check(self):
        if self.proc is not None and self.proc.poll() is not None:
            text = (self.folder/'runtime.log').read_text()[-4000:]
            raise RuntimeError('Runtime exited. See '+str(self.folder/'runtime.log')+'\n'+text)

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                # The supervisor forwards TERM and has its own bounded cleanup.
                self.proc.terminate()
                self.proc.wait(timeout=20)
        if self.log is not None:
            self.log.close()


class RosCollectionAdapter:
    def __init__(self, settings, runtime):
        self.settings, self.runtime = settings, runtime
        self.config = load_system(settings.runtime_config)
        self.bag_root = resolve(self.config['dataset'], self.config['dataset']['bag_root']).resolve()
        self.node = rclpy.create_node('collection_session_'+uuid.uuid4().hex[:8])
        self.mode, self.episode = None, None
        self.seen, self.diagnostics = {}, {}
        self.active, self.pending_start, self.claimed = None, None, False
        self.clients = {name: self.node.create_client(kind, name) for name, kind in [
            ('/set_control_mode', SetControlMode), ('/start_episode', StartEpisode),
            ('/stop_episode', StopEpisode), ('/controller_manager/list_controllers', ListControllers)]}
        self.diag_pub = self.node.create_publisher(DiagnosticArray, '/diagnostics', 10)
        self.subscriptions = [
            self.node.create_subscription(String, '/control_mode', lambda m: setattr(self, 'mode', m.data), LATCHED_QOS),
            self.node.create_subscription(EpisodeState, '/episode/state', lambda m: setattr(self, 'episode', m), LATCHED_QOS),
            self.node.create_subscription(DiagnosticArray, '/diagnostics', self.receive_diagnostics, 10)]
        for key, topic in [('leader', '/leader/joint_states'), ('follower', '/hardware/follower/state')]:
            self.subscriptions.append(self.node.create_subscription(JointState, topic,
                lambda m, k=key: self.receive_state(k, m), STATE_QOS))
        for name in self.config['camera']['cameras']:
            self.subscriptions.append(self.node.create_subscription(String, f'/camera/{name}/frame_metadata',
                lambda m, k=name: self.receive_camera(k, m), 30))

    def receive_state(self, key, msg):
        age = self.node.get_clock().now().nanoseconds/1e9-msg.header.stamp.sec-msg.header.stamp.nanosec/1e9
        if -.05 <= age <= self.config['safety']['state_timeout_s']:
            self.seen[key] = time.monotonic()

    def receive_camera(self, name, msg):
        data = json.loads(msg.data)
        age = (self.node.get_clock().now().nanoseconds-data['timestamp_ns'])/1e9
        if -.05 <= age <= self.config['camera']['timeout_s']:
            self.seen['camera_'+name] = time.monotonic()

    def receive_diagnostics(self, msg):
        for status in msg.status:
            self.diagnostics[status.name] = status

    def pump(self, timeout=.02, check_process=True):
        if check_process:
            self.runtime.check()
        rclpy.spin_once(self.node, timeout_sec=timeout)

    def wait(self, future, timeout=None, check_process=True):
        deadline = time.monotonic()+(timeout or self.settings.service_timeout_s)
        while not future.done() and time.monotonic() < deadline:
            self.pump(check_process=check_process)
        if not future.done():
            raise TimeoutError('ROS service response timed out')
        return future.result()

    def call(self, name, request, check_process=True):
        client = self.clients[name]
        if not client.wait_for_service(timeout_sec=1.):
            raise RuntimeError('Service unavailable: '+name)
        result = self.wait(client.call_async(request), check_process=check_process)
        if hasattr(result, 'success') and not result.success:
            raise RuntimeError(name+': '+result.message)
        return result

    def get_parameters(self, node_name, names):
        client = self.node.create_client(GetParameters, node_name+'/get_parameters')
        try:
            if not client.wait_for_service(timeout_sec=2.):
                raise RuntimeError('Runtime parameter service unavailable: '+node_name)
            result = self.wait(client.call_async(GetParameters.Request(names=names)))
            return [v.string_value for v in result.values]
        finally:
            self.node.destroy_client(client)

    def ready(self):
        deadline = time.monotonic()+self.settings.startup_timeout_s
        while time.monotonic() < deadline:
            self.pump(.05)
            if self.mode in ('FAULT', 'ESTOP'):
                raise RuntimeError('Runtime is '+self.mode+'; inspect diagnostics before collecting')
            if not all(c.service_is_ready() for c in self.clients.values()):
                continue
            now = time.monotonic()
            required = ['leader', 'follower', *('camera_'+k for k in self.config['camera']['cameras'])]
            if self.mode not in ('IDLE', 'TELEOP') or not all(now-self.seen.get(k, 0) < .4 for k in required):
                continue
            controllers = self.call('/controller_manager/list_controllers', ListControllers.Request()).controller
            if len(controllers) < 2 or any(c.state != 'active' for c in controllers):
                continue
            break
        else:
            raise TimeoutError('Runtime readiness timed out: need fresh robot/camera data and active controllers')
        if self.episode is not None and self.episode.state == 'RECORDING':
            raise RuntimeError('An episode is already recording; collection did not take ownership')
        actual_config, backend = self.get_parameters('/so101_follower_driver', ['config', 'backend'])
        if config_path(actual_config) != self.settings.runtime_config or backend != self.settings.backend:
            raise RuntimeError('Attached Runtime config/backend differs from collection YAML')
        for name in self.config['camera']['cameras']:
            actual = self.get_parameters('/camera_'+name, ['config'])[0]
            if config_path(actual) != self.settings.runtime_config:
                raise RuntimeError('Camera Runtime config differs: '+name)
        if self.settings.start_runtime:
            self.get_parameters('/foxglove_bridge', ['address'])

    def enter_teleop(self):
        if self.episode is not None and self.episode.state == 'RECORDING':
            raise RuntimeError('An episode is already recording (started while waiting); collection did not take ownership')
        # Record ownership before waiting so an interrupted reply still disarms.
        self.claimed = True
        self.call('/set_control_mode', SetControlMode.Request(mode='TELEOP'))
        deadline = time.monotonic()+2.
        while self.mode != 'TELEOP' and time.monotonic() < deadline:
            self.pump()
        self.check_health()

    def check_health(self):
        if self.mode != 'TELEOP':
            raise RuntimeError('Collection stopped: control mode is '+str(self.mode))
        now = time.monotonic()
        for key in ('leader', 'follower'):
            if now-self.seen.get(key, 0) > self.config['safety']['state_timeout_s']:
                raise RuntimeError('Collection stopped: stale '+key+' state')
        for name in self.config['camera']['cameras']:
            if now-self.seen.get('camera_'+name, 0) > self.config['camera']['timeout_s']:
                raise RuntimeError('Collection stopped: stale camera '+name)
        for name in ['data_recorder', 'so101_follower_driver', *('camera_'+k for k in self.config['camera']['cameras'])]:
            status = self.diagnostics.get(name)
            if status is not None and status.level >= DiagnosticStatus.ERROR:
                raise RuntimeError(name+': '+status.message)

    def resolve_start(self, result):
        if not result.success:
            raise RuntimeError('/start_episode: '+result.message)
        folder = (self.bag_root/result.episode_id).resolve()
        if folder.parent != self.bag_root:
            raise RuntimeError('Invalid episode path returned by Runtime')
        self.active = folder
        self.pending_start = None
        return folder

    def start_episode(self, task, operator):
        self.check_health()
        self.pending_start = self.clients['/start_episode'].call_async(
            StartEpisode.Request(task_id=task, operator_name=operator))
        return self.resolve_start(self.wait(self.pending_start))

    def stop_episode(self, success, reason, check_process=True):
        self.call('/stop_episode', StopEpisode.Request(success=success, failure_reason=reason), check_process)
        metadata = json.loads((self.active/'episode.json').read_text())
        self.active = None
        return metadata

    def cleanup(self, reason):
        metadata = None
        try:
            if self.pending_start is not None:
                # Do not cancel a start request that may already have opened a bag.
                self.resolve_start(self.wait(self.pending_start, check_process=False))
            if self.active is not None:
                metadata = self.stop_episode(False, reason, check_process=False)
        finally:
            if self.claimed and self.mode in ('IDLE', 'TELEOP'):
                self.call('/set_control_mode', SetControlMode.Request(mode='IDLE'), check_process=False)
                self.claimed = False
        return metadata

    def progress(self, phase, logical_episode, accepted, attempts, remaining, current):
        labels = {'READY': '開始待ち', 'WARMUP': '練習中・記録なし', 'RECORDING': '記録中',
                  'RESET': '配置を戻す・記録なし', 'COMPLETE': '収集完了', 'STOPPED': '収集終了',
                  'INTERRUPTED': '収集中断', 'ERROR': '収集エラー'}
        values = {'phase': phase, 'task': self.settings.task,
                  'episode': f'{logical_episode}/{self.settings.num_episodes}',
                  'accepted_episodes': accepted, 'target_episodes': self.settings.num_episodes,
                  'attempts': attempts, 'remaining_s': f'{remaining:.1f}',
                  'recording': phase == 'RECORDING',
                  'episode_id': current['episode_id'] if current else '',
                  'keys': '→/s: 保存・次へ  ←/r: やり直し  f: 失敗  Esc/q: 終了'}
        msg = DiagnosticArray(status=[DiagnosticStatus(name='collection_session', hardware_id='so101',
            level=DiagnosticStatus.ERROR if phase == 'ERROR' else DiagnosticStatus.OK,
            message=labels.get(phase, phase), values=[KeyValue(key=k, value=str(v)) for k, v in values.items()])])
        msg.header.stamp = self.node.get_clock().now().to_msg()
        self.diag_pub.publish(msg)


def main():
    parser = argparse.ArgumentParser(description='Foxgloveを見ながらLeRobot形式の時間・本数・キー操作でROS Episodeを収集')
    parser.add_argument('--config_path', '--config-path', default=str(PROJECT/'config/collection/record.yaml'))
    parser.add_argument('--attach', action='store_true', help='既存の同じ設定のRuntimeに接続し、終了時もRuntimeを残す')
    parser.add_argument('--backend', choices=['real', 'mock', 'mujoco'])
    parser.add_argument('--num-episodes', type=int)
    parser.add_argument('--episode-time-s', type=float)
    parser.add_argument('--reset-time-s', type=float)
    parser.add_argument('--task')
    parser.add_argument('--operator')
    parser.add_argument('--yes', action='store_true', help='開始時のEnter待ちを省略する（自動実行用）')
    args = parser.parse_args()
    try:
        settings = CollectionSettings.from_file(args.config_path, backend=args.backend,
            start_runtime=False if args.attach else None, num_episodes=args.num_episodes,
            episode_time_s=args.episode_time_s, reset_time_s=args.reset_time_s,
            task=args.task, operator=args.operator)
    except (ValueError, KeyError, OSError, TypeError) as error:
        parser.error(str(error))
    if not sys.stdin.isatty() and not args.yes:
        parser.error('対話端末で実行してください。SSHは ssh -t、コンテナは docker exec -it を使います。')
    (PROJECT/'data').mkdir(exist_ok=True)
    with (PROJECT/'data/.collection.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another collection script is running')
        journal = CollectionJournal(settings)
        runtime = RuntimeProcess(settings, journal.folder)
        # Keep ROS alive while Ctrl+C cleanup closes the bag and selects IDLE.
        # rclpy's default SIGINT handler shuts down the context too early.
        rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
        adapter = RosCollectionAdapter(settings, runtime)
        stopping = False
        def request_stop(signum, frame):
            nonlocal stopping
            if stopping:
                return
            stopping = True
            raise KeyboardInterrupt('signal '+str(signum))
        previous_int = signal.signal(signal.SIGINT, request_stop)
        previous_term = signal.signal(signal.SIGTERM, request_stop)
        runner_started = False
        exit_code = 0
        try:
            if settings.start_runtime:
                if adapter.clients['/set_control_mode'].wait_for_service(timeout_sec=.5):
                    raise RuntimeError('Runtime already running. Use --attach with its matching config, or stop it first.')
                runtime.start()
            adapter.ready()
            print('Foxglove: ws://127.0.0.1:'+os.environ.get('SO101_FOXGLOVE_PORT', '8765')+
                  '（MacからはSSHトンネル）', flush=True)
            print('Layout: config/foxglove/so101-collect-h264.json' if adapter.config['camera'].get('video',{}).get('enabled')
                  else 'Layout: config/foxglove/so101-collect.json', flush=True)
            print(f'作業: {settings.task}\n収集: {settings.num_episodes}本 / 各{settings.episode_time_s}秒 '
                  f'/ 戻し{settings.reset_time_s}秒\n保存一覧: {journal.folder}/session.json', flush=True)
            print('→/s/Enter: 保存・次へ  ←/r: やり直し  f: 失敗  Esc/q: 終了\n'
                  '時間切れは成功として保存します。失敗はf、やり直しは←を押してください。\n'
                  'キーはこのターミナルにフォーカスして操作します。終了時はIDLE・トルクOFFになります。', flush=True)
            with TerminalKeys() as keys:
                if not args.yes:
                    print('Foxgloveの映像と初期配置を確認し、EnterでTELEOPと収集を開始します。', flush=True)
                    deadline = 0.
                    while True:
                        adapter.pump(.03)
                        if adapter.mode in ('FAULT', 'ESTOP'):
                            raise RuntimeError('Runtime is '+adapter.mode)
                        if time.monotonic() > deadline:
                            adapter.progress('READY', 1, 0, 0, 0., None)
                            deadline = time.monotonic()+1.
                        key = keys.read()
                        if key == 'quit':
                            journal.end('stopped', 'Cancelled before TELEOP')
                            break
                        if key == 'save':
                            runner_started = True
                            CollectionRunner(settings, adapter, keys, journal).run()
                            break
                else:
                    runner_started = True
                    CollectionRunner(settings, adapter, keys, journal).run()
        except KeyboardInterrupt:
            exit_code = 130
            if not runner_started:
                journal.end('interrupted', 'KeyboardInterrupt')
        except Exception as error:
            print(str(error), file=sys.stderr, flush=True)
            exit_code = 1
            if not runner_started:
                journal.end('error', str(error))
        finally:
            try:
                if not runner_started:
                    adapter.cleanup('Collection startup interrupted')
            finally:
                try:
                    runtime.stop()
                finally:
                    adapter.node.destroy_node()
                    if rclpy.ok():
                        rclpy.shutdown()
                    signal.signal(signal.SIGINT, previous_int)
                    signal.signal(signal.SIGTERM, previous_term)
            print(f'終了: {len(journal.accepted)}/{settings.num_episodes}本を選択。{journal.folder}/session.json', flush=True)
        raise SystemExit(exit_code)

"""Resident model switching with synchronized ROS inputs; no robot or USB access."""
import json
import os
import time
import numpy as np
import pytest

pytest.importorskip('torch')
rclpy = pytest.importorskip('rclpy')
from test_methods_integration import trained_methods

pytestmark = [pytest.mark.ros, pytest.mark.ml,
              pytest.mark.skipif(os.environ.get('RUN_ML_TESTS') != '1', reason='Set RUN_ML_TESTS=1')]


def test_resident_pool_switches_all_conditions_only_in_idle_and_discards_history(trained_methods, monkeypatch):
    from rclpy.executors import SingleThreadedExecutor
    from sensor_msgs.msg import Image, JointState
    from std_msgs.msg import String, Bool
    from trajectory_msgs.msg import JointTrajectory
    from so101_msgs.srv import SelectPolicy
    from std_srvs.srv import Trigger
    from so101_core.ros_utils import STATE_QOS, COMMAND_QOS, LATCHED_QOS, joint_state
    from so101_policy.benchmark_node import BenchmarkPolicyNode
    root, folder, native, exported, models, correction = trained_methods
    monkeypatch.setenv('ROS_DOMAIN_ID', '87')
    rclpy.init(args=['--ros-args', '-p', f'config:={models["standard"]}/runtime/system.yaml',
        '-p', 'policy_backend:=flow', '-p', 'model_id:=standard-test',
        '-p', 'asynchronous_inference:=true',
        '-p', 'benchmark_models:=\'["standard-test", "vlash-test"]\''])
    policy = BenchmarkPolicyNode()
    peer = rclpy.create_node('resident_pool_test')
    executor = SingleThreadedExecutor(); executor.add_node(policy); executor.add_node(peer)
    received = {'condition': None}; actions = []
    peer.create_subscription(String, '/policy/condition',
        lambda m: received.update(condition=json.loads(m.data)), LATCHED_QOS)
    peer.create_subscription(JointTrajectory, '/policy/action', lambda m: actions.append(m), COMMAND_QOS)
    states = peer.create_publisher(JointState, '/hardware/follower/state', STATE_QOS)
    modes = peer.create_publisher(String, '/control_mode', LATCHED_QOS)
    images = {n: peer.create_publisher(Image, '/camera/'+n+'/image_raw', STATE_QOS) for n in ['top', 'arm']}
    def publish():
        msg = joint_state(peer, policy.joints, [.02]*6); states.publish(msg)
        for name, pub in images.items():
            image = Image(height=64, width=64, encoding='rgb8', step=192, data=bytes([50])*(64*64*3))
            image.header = msg.header; pub.publish(image)
    timer = peer.create_timer(1/30, publish)
    def until(predicate, timeout=10):
        end = time.monotonic()+timeout
        while not predicate() and time.monotonic()<end: executor.spin_once(timeout_sec=.005)
        assert predicate(), (policy.mode, policy.ready, policy.engine_failed, received)
    client = peer.create_client(SelectPolicy, '/policy/select')
    def select(model, smoothing):
        future = client.call_async(SelectPolicy.Request(model_id=model, smoothing=smoothing))
        until(future.done)
        return future.result()
    identities = {n: id(p) for n, (p, spec) in policy.pool.items()}
    try:
        modes.publish(String(data='IDLE')); until(lambda: policy.ready)
        assert actions == []
        fresh = policy.check_ready(Trigger.Request(), Trigger.Response())
        assert fresh.success
        # A completed warm-up must not authorize motion after raw inputs age.
        sample = policy.images['top']
        policy.images['top'] = (time.monotonic()-1., sample[1], sample[2])
        stale = policy.check_ready(Trigger.Request(), Trigger.Response())
        assert not stale.success and not policy.ready
        assert 'Camera observation stale: top' in json.loads(stale.message)['error']
        assert not actions
        until(lambda: policy.ready)
        for number, (model, smoothing) in enumerate([
                ('standard-test', 'none'), ('vlash-test', 'three_point'),
                ('standard-test', 'three_point'), ('vlash-test', 'none')], 1):
            response = select(model, smoothing)
            while not response.success and 'Inference busy' in response.message:
                executor.spin_once(timeout_sec=.01); response = select(model, smoothing)
            assert response.success, response.message
            until(lambda: received['condition'] and received['condition']['generation'] == response.generation
                  and received['condition']['ready'])
            assert policy.model_id == model and policy.engine.smoothing == smoothing
            assert policy.engine.chunk is None
            assert not policy.engine.smoother.inputs and not policy.engine.smoother.outputs
            assert {n: id(p) for n, (p, spec) in policy.pool.items()} == identities
            count = len(actions)
            modes.publish(String(data='AUTONOMOUS')); until(lambda: len(actions) >= count+12)
            assert not policy.engine_failed
            assert not select(model, smoothing).success  # Cannot change even same model during motion.
            modes.publish(String(data='IDLE')); until(lambda: policy.mode == 'IDLE')
            count = len(actions)
            end = time.monotonic()+.15
            while time.monotonic()<end: executor.spin_once(timeout_sec=.01)
            assert len(actions) == count
        assert all(np.isfinite(a.points[0].positions).all() for a in actions)
    finally:
        peer.destroy_timer(timer); executor.shutdown(); policy.destroy_node(); peer.destroy_node(); rclpy.shutdown()


@pytest.mark.parametrize('stop_after_first', [False, True])
def test_interactive_mock_session_records_four_conditions_compressed_and_exports(trained_methods, tmp_path, stop_after_first):
    import pathlib
    import pty
    import socket
    import re
    import signal
    import subprocess
    import sys
    import yaml
    root, folder, native, exported, models, correction = trained_methods
    project = pathlib.Path(__file__).resolve().parents[1]
    import shutil
    from so101_core.converter import sha256_file
    # Keep full 640x480 / 30 Hz inputs: small frames hide missing DDS transport
    # profiles because only full raw frames exceed the default SHM segment.
    model_root = tmp_path/'models'
    for method in ('standard', 'vlash'):
        target = model_root/(method+'-test')
        shutil.copytree(models[method], target)
        # Native training's legacy metadata predates the comparison's required fields.
        # Supply the exact synthetic fixture settings, without changing weights.
        train_path = target/'checkpoint/train_config.json'
        train = json.loads(train_path.read_text())
        train.update(policy='native_flow', batch_size=2, training_frames=20,
                     policy_overrides={'dim_model':16, 'n_heads':2, 'n_layers':1, 'denoise_steps':4})
        train_path.write_text(json.dumps(train))
        manifest_path = target/'deployment.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['training_config'] = train
        manifest['checkpoint_sha256']['train_config.json'] = sha256_file(train_path)
        manifest_path.write_text(json.dumps(manifest))
        for policy_path in (target/'runtime').glob('policy*.yaml'):
            spec = json.loads(policy_path.read_text()); spec['model_root'] = str(model_root)
            policy_path.write_text(json.dumps(spec))
    # Small, independently trained networks exercise the actual CLI, pool, controllers,
    # recorder and keyboard lifecycle. These are never task-success measurements.
    cfg = {'backend': 'mock', 'models': {'standard': 'standard-test', 'vlash': 'vlash-test'},
           'trials_per_condition': 1, 'seed': 42, 'max_duration_s': 3., 'reset_time_s': 1.,
           'operator': 'synthetic', 'success_rule': 'SYNTHETIC TEST ONLY',
           'output_root': str(tmp_path/'sessions'), 'startup_timeout_s': 40, 'service_timeout_s': 10}
    path = tmp_path/'benchmark.yaml'; path.write_text(yaml.safe_dump(cfg))
    # Mock control uses a private supervisor/lock, so tests cannot interrupt
    # an operator's calibration or acquire the physical robot compute lock.
    test_project = tmp_path/'project'
    (test_project/'scripts').mkdir(parents=True)
    (test_project/'config/runtime').mkdir(parents=True)
    shutil.copy2(project/'config/runtime/fastdds.xml', test_project/'config/runtime/fastdds.xml')
    supervisor = (project/'scripts/runtime_supervisor.py').read_text().replace(
        '/workspace/data', str(test_project/'data'))
    (test_project/'scripts/runtime_supervisor.py').write_text(supervisor)
    (test_project/'data').mkdir()
    bootstrap = ('import importlib.util, sys; from pathlib import Path; '
        'source=Path(sys.argv.pop(1)); project=Path(sys.argv.pop(1)); '
        'spec=importlib.util.spec_from_file_location("benchmark_client_test",source); '
        'module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); '
        'module.PROJECT=project; module.main()')
    env = dict(os.environ, ROS_DOMAIN_ID='88', SO101_FOXGLOVE_PORT='18766', SO101_USE_LOCAL_CONFIG='0')
    master, slave = pty.openpty()
    logfile = tmp_path/'client.log'
    with logfile.open('w') as log:
        proc = subprocess.Popen([sys.executable, '-c', bootstrap, str(project/'scripts/benchmark.py'), str(test_project), '--config', str(path),
            '--model-root', str(model_root)], stdin=slave, stdout=log,
            stderr=subprocess.STDOUT, env=env, start_new_session=True)
        os.close(slave)
        sent, began = set(), {}
        connected = False
        try:
            deadline = time.monotonic()+70
            while proc.poll() is None and time.monotonic()<deadline:
                text = logfile.read_text()
                if 'Enter/→/sで開始' in text and 'start' not in sent:
                    if not connected:
                        with socket.create_connection(('127.0.0.1', 18766), timeout=2) as sock:
                            sock.settimeout(2)
                            sock.sendall(b'GET / HTTP/1.1\r\nHost: 127.0.0.1:18766\r\nUpgrade: websocket\r\n'
                                b'Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
                                b'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: foxglove.sdk.v1\r\n\r\n')
                            header = b''
                            while not header.endswith(b'\r\n\r\n'):
                                chunk = sock.recv(1)
                                assert chunk
                                header += chunk
                            assert b'101 Switching Protocols' in header
                            connected = True
                    began.setdefault('ready', time.monotonic())
                    # Readiness alone is insufficient: exercise an IDLE wait too.
                    if time.monotonic()-began['ready'] > 1.:
                        os.write(master, b'\r'); sent.add('start')
                phases = list(re.finditer(r'(RUNNING|RESET)  試行 (\d+)/4[^\n]*', text))
                if phases:
                    phase, trial = phases[-1].group(1), int(phases[-1].group(2))
                    key = (phase, trial); began.setdefault(key, time.monotonic())
                    elapsed = time.monotonic()-began[key]
                    if phase == 'RUNNING' and trial not in sent and elapsed>.5:
                        if trial in (1,2,3):
                            os.write(master, b'q' if stop_after_first else {1:b's', 2:b'f', 3:b'p'}[trial]); sent.add(trial)
                    if phase == 'RESET' and trial == 3 and 'resume' not in sent and elapsed>1.5:
                        os.write(master, b'p'); sent.add('resume')
                time.sleep(.02)
            assert proc.poll() is not None, logfile.read_text()
            assert proc.returncode == 0, logfile.read_text()
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT); proc.wait(timeout=30)
            os.close(master)
    session_path = next((tmp_path/'sessions').glob('*/session.json'))
    assert connected
    bridge_log = (session_path.parent/'runtime.log').read_text()
    for topic in ('/benchmark/state', '/benchmark/events', '/policy/condition'):
        assert 'topic "'+topic+'"' in bridge_log
    session = json.loads(session_path.read_text())
    assert session['status'] == ('stopped' if stop_after_first else 'complete')
    assert [t['outcome'] for t in session['trials']] == (['aborted'] if stop_after_first else ['success', 'failure', 'aborted', 'failure'])
    assert len({t['condition'] for t in session['trials']}) == (1 if stop_after_first else 4)
    assert len({t['condition_generation'] for t in session['trials']}) == (1 if stop_after_first else 4)
    for trial in session['trials']:
        bag = pathlib.Path(trial['recording'])
        assert list((bag/'raw').glob('*.mcap')) and (bag/'raw/message_index.jsonl.gz').is_file()
        metadata = json.loads((bag/'benchmark.json').read_text())
        assert metadata['trial']['trial_id'] == trial['trial_id']
        assert metadata['start_timestamp_ns'] < metadata['end_timestamp_ns']
        checked = metadata['inference_inputs_at_start']
        assert checked['model_id'] == metadata['model_id']
        assert all(age <= checked['max_age_s'] for age in checked['camera_receive_age_s'].values())
        summary = json.loads((bag/'recording_summary.json').read_text())
        for name in ('top', 'side', 'arm'):
            assert summary['/camera/'+name+'/video']['messages'] > 0
        assert not any(n.endswith('/image_raw') for n in summary)
        assert summary['/hardware/follower/state']['messages'] > 0
        assert summary['/policy/action']['messages'] > 0
        assert summary['/benchmark/events']['messages'] > 0
        assert json.loads((bag/'episode.json').read_text())['status'] == 'complete'
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from diagnostic_msgs.msg import DiagnosticArray
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(bag/'raw'), storage_id='mcap'),
                    rosbag2_py.ConverterOptions('cdr','cdr'))
        progress = []
        while reader.has_next():
            topic, payload, timestamp = reader.read_next()
            if topic == '/diagnostics':
                for status in deserialize_message(payload, DiagnosticArray).status:
                    if status.name == 'benchmark_session':
                        progress.append({v.key:v.value for v in status.values})
        assert any(v['condition'] == trial['condition'] and v['phase'] == 'RUNNING' for v in progress)
    report = json.loads((session_path.parent/'report/results.json').read_text())
    assert report['complete'] == (not stop_after_first)
    assert sum(c['attempts'] for c in report['conditions']) == (1 if stop_after_first else 4)
    assert sum(c['aborted'] for c in report['conditions']) == 1
    events = [json.loads(line) for line in (session_path.parent/'events.jsonl').read_text().splitlines()]
    if not stop_after_first:
        assert any(e['event'] == 'reset_resume' for e in events)

"""Collection controls on an isolated, fully mocked ROS Runtime; no USB access."""
import json
import os
from pathlib import Path
import pty
import signal
import shutil
import socket
import subprocess
import sys
import time

import pytest
import yaml

rclpy = pytest.importorskip('rclpy')
from diagnostic_msgs.msg import DiagnosticArray
from so101_msgs.srv import SetControlMode, StartEpisode, StopEpisode
from so101_core.config import load_system
from so101_core.converter import convert
from test_runtime import runtime

pytestmark = pytest.mark.ros


@pytest.mark.parametrize('runtime', [{'profile': 'runtime/system.real30hz.640x480.h264.yaml'}], indirect=True)
def test_collection_keys_redo_mcap_selection_and_dataset_conversion(runtime):
    node, state, call, spin_until, folder, tmp, runtime_process = runtime
    source = Path(__file__).resolve().parents[1]
    cfg = {'runtime': {'backend': 'mock', 'config': 'runtime/system.real30hz.640x480.h264.yaml', 'start': False},
           'dataset': {'single_task': '消しゴムを箱に入れる', 'operator': 'pytest', 'num_episodes': 2,
                       'episode_time_s': 1.3, 'reset_time_s': .8},
           'warmup_time_s': .1, 'session_root': '../collections'}
    config_path = folder/'record.yaml'
    config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True))
    seen = []
    def receive(msg):
        for status in msg.status:
            if status.name == 'collection_session':
                seen.append({v.key: v.value for v in status.values})
    node.create_subscription(DiagnosticArray, '/diagnostics', receive, 10)
    master, slave = pty.openpty()
    logfile = tmp/'collection.log'
    with logfile.open('w') as log:
        proc = subprocess.Popen([sys.executable, str(source/'scripts/collect.py'), '--config_path', str(config_path)],
                                stdin=slave, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            def until(predicate, seconds=25):
                end = time.monotonic()+seconds
                while not predicate() and time.monotonic()<end:
                    rclpy.spin_once(node, timeout_sec=.02)
                    if proc.poll() is not None:
                        break
                assert predicate(), logfile.read_text()
            until(lambda: 'EnterでTELEOP' in logfile.read_text())
            os.write(master, b'\r')
            until(lambda: any(v.get('phase') == 'RECORDING' and v['attempts'] == '1' for v in seen))
            os.write(master, b'\x1b[D')
            until(lambda: any(v.get('phase') == 'RECORDING' and v['attempts'] == '2' for v in seen))
            # Preserve enough samples for a meaningful video/state conversion.
            end = time.monotonic()+.6
            while time.monotonic()<end:rclpy.spin_once(node,timeout_sec=.02)
            os.write(master, b'\x1b[C')
            until(lambda: any(v.get('phase') == 'RESET' and v['attempts'] == '2' for v in seen))
            os.write(master, b'\x1b[D')
            until(lambda: proc.poll() is not None, seconds=30)
            assert proc.returncode == 0, logfile.read_text()
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=20)
            os.close(master)
            os.close(slave)
    spin_until(lambda: state['mode'] == 'IDLE' and not state['enabled'])
    assert runtime_process.poll() is None  # attach mode leaves the Runtime alive
    session_path = next((tmp/'collections').glob('*/session.json'))
    session = json.loads(session_path.read_text())
    assert session['status'] == 'complete' and len(session['accepted_episodes']) == 2
    assert len(session['attempts']) == 4
    assert [item['accepted'] for item in session['attempts']] == [False, False, True, True]
    for item in session['attempts']:
        episode = Path(item['path'])
        assert json.loads((episode/'episode.json').read_text())['status'] == 'complete'
        assert list((episode/'raw').glob('*.mcap'))
    excluded = Path(session['attempts'][1]['path'])
    assert json.loads((excluded/'episode.json').read_text())['success']
    with pytest.raises(ValueError, match='excluded by collection'):
        convert([excluded], tmp/'excluded-dataset', 'local/excluded', load_system(folder/'runtime/system.real30hz.640x480.h264.yaml'))
    # Exercise the same collection-to-training command documented for the user.
    converted = subprocess.run([sys.executable, '-m', 'so101_core.converter', '--collection', str(session_path),
                               '--output', str(tmp/'accepted-dataset'), '--dataset-name', 'local/collection-test'],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=40)
    assert converted.returncode == 0, converted.stdout
    provenance = json.loads((tmp/'accepted-dataset/provenance.json').read_text())
    assert {e['episode_id'] for e in provenance['episodes']} == {Path(p).name for p in session['accepted_episodes']}


@pytest.mark.parametrize('runtime', [{'profile': 'runtime/system.real30hz.640x480.h264.yaml'}], indirect=True)
@pytest.mark.parametrize('stop_signal', [signal.SIGINT, signal.SIGTERM])
def test_ctrl_c_closes_own_episode_and_attach_keeps_runtime_idle(runtime, stop_signal):
    node, state, call, spin_until, folder, tmp, runtime_process = runtime
    source = Path(__file__).resolve().parents[1]
    config = {'runtime': {'backend': 'mock', 'config': 'runtime/system.real30hz.640x480.h264.yaml', 'start': False},
              'dataset': {'single_task': 'interrupted', 'operator': 'pytest', 'num_episodes': 1,
                          'episode_time_s': 30, 'reset_time_s': 0},
              'warmup_time_s': 0, 'session_root': '../collections'}
    path = folder/'record.yaml'
    path.write_text(yaml.safe_dump(config))
    with (tmp/'interrupted-collection.log').open('w') as log:
        proc = subprocess.Popen([sys.executable, str(source/'scripts/collect.py'), '--config_path', str(path), '--yes'],
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            spin_until(lambda: any((tmp/'data/bags').glob('*/raw/*.mcap')), timeout=20)
            proc.send_signal(stop_signal)
            deadline = time.monotonic()+20
            while proc.poll() is None and time.monotonic()<deadline:rclpy.spin_once(node,timeout_sec=.02)
            assert proc.poll() == 130, (tmp/'interrupted-collection.log').read_text()
        finally:
            if proc.poll() is None:
                proc.kill();proc.wait()
    spin_until(lambda: state['mode'] == 'IDLE' and not state['enabled'])
    assert runtime_process.poll() is None
    session = json.loads(next((tmp/'collections').glob('*/session.json')).read_text())
    assert session['status'] == 'interrupted' and not session['accepted_episodes']
    episodes = list((tmp/'data/bags').glob('*/episode.json'))
    assert len(episodes) == 1
    metadata = json.loads(episodes[0].read_text())
    assert metadata['status'] == 'complete' and not metadata['success']
    if stop_signal == signal.SIGINT:
        # A second collection client must leave someone else's recording and
        # control mode intact when attachment is refused.
        assert call(SetControlMode, '/set_control_mode', mode='TELEOP').success
        external = call(StartEpisode, '/start_episode', task_id='external', operator_name='other')
        assert external.success
        blocked = subprocess.run([sys.executable, str(source/'scripts/collect.py'), '--config_path', str(path), '--yes'],
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
        assert blocked.returncode == 1 and 'already recording' in blocked.stdout
        end = time.monotonic()+.3
        while time.monotonic()<end:rclpy.spin_once(node,timeout_sec=.02)
        assert state['mode'] == 'TELEOP' and state['enabled']
        assert call(StopEpisode, '/stop_episode', success=False, failure_reason='external test finished').success
        assert call(SetControlMode, '/set_control_mode', mode='IDLE').success


def test_single_command_starts_mock_runtime_bridge_and_stops_owned_processes(tmp_path):
    source = Path(__file__).resolve().parents[1]
    project = tmp_path/'project'
    (project/'scripts').mkdir(parents=True)
    # Keep mock validation locks/PIDs separate from any real Runtime. The
    # supervisor otherwise runs unchanged, including its process-group cleanup.
    supervisor = (source/'scripts/runtime_supervisor.py').read_text()
    supervisor = supervisor.replace('/workspace/data', str(project/'data'))
    (project/'scripts/runtime_supervisor.py').write_text(supervisor)
    folder = project/'config'
    shutil.copytree(source/'config', folder)
    camera_path = folder/'cameras/camera.640x480.h264.yaml'
    camera = yaml.safe_load(camera_path.read_text())
    for spec in camera['cameras'].values():spec.update(width=160, height=120)
    camera_path.write_text(yaml.safe_dump(camera))
    config = {'runtime': {'backend': 'mock', 'config': 'runtime/system.real30hz.640x480.h264.yaml', 'start': True},
              'dataset': {'single_task': 'owned mock collection', 'operator': 'pytest', 'num_episodes': 1,
                          'episode_time_s': 3, 'reset_time_s': .2},
              'warmup_time_s': .1, 'session_root': '../collections'}
    path = folder/'record.yaml'
    path.write_text(yaml.safe_dump(config))
    env = dict(os.environ, ROS_DOMAIN_ID='94', SO101_FOXGLOVE_BIND_ADDRESS='127.0.0.1', SO101_FOXGLOVE_PORT='18765')
    # Only the test swaps the project location; production always uses its own
    # shared compute lock. This command still executes the real main() workflow.
    bootstrap = ('import sys; from pathlib import Path; from so101_dataset import collect; '
                 'collect.PROJECT=Path(sys.argv.pop(1)); collect.main()')
    with (tmp_path/'collection.log').open('w') as log:
        proc = subprocess.Popen([sys.executable, '-c', bootstrap, str(project), '--config_path', str(path), '--yes'],
                                env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        connected = False
        try:
            deadline = time.monotonic()+35
            while time.monotonic()<deadline and proc.poll() is None:
                try:
                    with socket.create_connection(('127.0.0.1', 18765), timeout=.2) as sock:
                        sock.settimeout(2)
                        sock.sendall(b'GET / HTTP/1.1\r\nHost: 127.0.0.1:18765\r\nUpgrade: websocket\r\n'
                                     b'Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
                                     b'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: foxglove.sdk.v1\r\n\r\n')
                        connected = b'101 Switching Protocols' in sock.recv(4096)
                        if connected:break
                except OSError:
                    pass
                time.sleep(.1)
            proc.wait(timeout=30)
            assert proc.returncode == 0, (tmp_path/'collection.log').read_text()
            assert connected
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=30)
    session = json.loads(next((project/'collections').glob('*/session.json')).read_text())
    assert session['status'] == 'complete' and len(session['accepted_episodes']) == 1
    assert not list((project/'data/run').glob('*.pid'))
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', 18765), timeout=.2)

import json
import os
from pathlib import Path
import pty
import termios
import time

import pytest
import yaml

from so101_core.collection import CollectionSettings, CollectionJournal, CollectionRunner, TerminalKeys


def settings(tmp_path, count=2):
    return CollectionSettings(tmp_path/'system.yaml', 'mock', False, '消しゴムを箱に入れる',
                              'pytest', count, 1., 1., 0., tmp_path/'collections')


class FakeAdapter:
    def __init__(self, root):
        self.root, self.now = root, 0.
        self.active, self.idle, self.phase_name = None, False, None
        self.phases, self.attempts, self.stops = {}, [], []
        self.fail_on_recording = False

    def enter_teleop(self):
        self.idle = False

    def pump(self, timeout):
        self.now += .1

    def check_health(self):
        if self.fail_on_recording and self.phase_name == 'RECORDING':
            raise RuntimeError('Follower disconnected')

    def progress(self, phase, *args):
        if phase != self.phase_name:
            self.phases[phase] = self.phases.get(phase, 0)+1
            self.phase_name = phase

    def start_episode(self, task, operator):
        folder = self.root/f'episode_{len(self.attempts)}'
        folder.mkdir()
        (folder/'raw.mcap').write_bytes(b'original capture bytes')
        self.attempts.append(folder)
        self.active = folder
        return folder

    def stop_episode(self, success, reason):
        metadata = {'episode_id': self.active.name, 'status': 'complete', 'success': success,
                    'failure_reason': reason, 'bag_uri': str(self.active/'raw')}
        (self.active/'episode.json').write_text(json.dumps(metadata))
        self.stops.append(metadata)
        self.active = None
        return metadata

    def cleanup(self, reason):
        self.idle = True
        return self.stop_episode(False, reason) if self.active else None


class PlannedKeys:
    def __init__(self, adapter, events):
        self.adapter, self.events = adapter, events.copy()

    def read(self):
        a = self.adapter
        return self.events.pop((a.phase_name, a.phases.get(a.phase_name)), None)


def test_redo_after_save_excludes_original_without_rewriting_outcome_or_raw(tmp_path):
    config = settings(tmp_path)
    journal, adapter = CollectionJournal(config), FakeAdapter(tmp_path)
    # First episode is saved then rejected during reset. Its original outcome
    # stays true, while the collection annotation excludes it from training.
    keys = PlannedKeys(adapter, {('RECORDING', 1): 'save', ('RESET', 1): 'redo'})
    CollectionRunner(config, adapter, keys, journal, output=lambda *a: None,
                     clock=lambda: adapter.now).run()
    assert adapter.idle and adapter.active is None
    assert journal.data['status'] == 'complete'
    assert len(journal.data['attempts']) == 3 and len(journal.accepted) == 2
    first = journal.data['attempts'][0]
    assert first['status'] == 'rerecorded' and not first['accepted']
    assert json.loads((Path(first['path'])/'episode.json').read_text())['success']
    assert not json.loads((Path(first['path'])/'collection.json').read_text())['accepted']
    assert [x['logical_episode'] for x in journal.data['attempts']] == [1, 1, 2]
    for path in adapter.attempts:
        assert (path/'raw.mcap').read_bytes() == b'original capture bytes'


@pytest.mark.parametrize('event', ['redo', 'fail'])
def test_failed_attempt_does_not_increment_target_count(tmp_path, event):
    config = settings(tmp_path, count=1)
    journal, adapter = CollectionJournal(config), FakeAdapter(tmp_path)
    keys = PlannedKeys(adapter, {('RECORDING', 1): event})
    CollectionRunner(config, adapter, keys, journal, output=lambda *a: None,
                     clock=lambda: adapter.now).run()
    assert len(adapter.stops) == 2 and not adapter.stops[0]['success'] and adapter.stops[1]['success']
    assert len(journal.accepted) == 1 and journal.data['status'] == 'complete'


@pytest.mark.parametrize('failure', ['quit', 'fault'])
def test_interrupted_recording_is_closed_as_failed_and_disarmed(tmp_path, failure):
    config = settings(tmp_path)
    journal, adapter = CollectionJournal(config), FakeAdapter(tmp_path)
    adapter.fail_on_recording = failure == 'fault'
    keys = PlannedKeys(adapter, {('RECORDING', 1): 'quit'} if failure == 'quit' else {})
    runner = CollectionRunner(config, adapter, keys, journal, output=lambda *a: None,
                              clock=lambda: adapter.now)
    if failure == 'fault':
        with pytest.raises(RuntimeError, match='Follower disconnected'):
            runner.run()
    else:
        runner.run()
    assert adapter.idle and adapter.active is None and len(journal.accepted) == 0
    assert len(adapter.stops) == 1 and not adapter.stops[0]['success']
    assert json.loads((journal.folder/'session.json').read_text())['status'] == ('error' if failure == 'fault' else 'stopped')


def test_terminal_decodes_fragmented_arrows_and_restores_terminal():
    master, slave = pty.openpty()
    try:
        with os.fdopen(os.dup(slave), 'r') as stream:
            before = termios.tcgetattr(slave)
            with TerminalKeys(stream) as keys:
                os.write(master, b'\x1b')
                time.sleep(.01)
                assert keys.read() is None
                os.write(master, b'[')
                time.sleep(.01)
                assert keys.read() is None
                os.write(master, b'C')
                time.sleep(.01)
                assert keys.read() == 'save'
                os.write(master, b'\x1bOD')
                time.sleep(.01)
                assert keys.read() == 'redo'
                os.write(master, b'f')
                time.sleep(.01)
                assert keys.read() == 'fail'
                os.write(master, b'\x1b')
                time.sleep(.01)
                assert keys.read() is None
                time.sleep(.11)
                assert keys.read() == 'quit'
            assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


def test_collection_paths_and_invalid_settings(tmp_path):
    folder = tmp_path/'config'
    folder.mkdir()
    (folder/'system.yaml').write_text('files: {}')
    config = {'runtime': {'backend': 'mock', 'config': 'system.yaml'},
              'dataset': {'single_task': 'task', 'operator': 'pytest', 'num_episodes': 2,
                          'episode_time_s': 1, 'reset_time_s': 0}}
    path = folder/'record.yaml'
    path.write_text(yaml.safe_dump(config))
    loaded = CollectionSettings.from_file(path)
    assert loaded.runtime_config == folder/'system.yaml'
    assert loaded.session_root == tmp_path/'data/collections'
    for value in (0, -1, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            CollectionSettings.from_file(path, episode_time_s=value)
    with pytest.raises(ValueError):
        CollectionSettings.from_file(path, num_episodes=True)

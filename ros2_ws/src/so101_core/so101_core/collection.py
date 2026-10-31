"""Timed episode collection, independent of ROS and hardware access."""
import json
import math
import os
import select
import sys
import termios
import time
import tty
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config, resolve, config_path


@dataclass
class CollectionSettings:
    runtime_config: Path
    backend: str
    start_runtime: bool
    task: str
    operator: str
    num_episodes: int
    episode_time_s: float
    reset_time_s: float
    warmup_time_s: float
    session_root: Path
    startup_timeout_s: float = 45.
    service_timeout_s: float = 15.

    @classmethod
    def from_file(cls, path, **overrides):
        config = load_config(path)
        runtime, dataset = config['runtime'], config['dataset']
        values = dict(runtime_config=config_path(resolve(config, runtime['config'])),
                      backend=runtime.get('backend', 'real'), start_runtime=runtime.get('start', True),
                      task=dataset['single_task'], operator=dataset['operator'],
                      num_episodes=dataset['num_episodes'], episode_time_s=dataset['episode_time_s'],
                      reset_time_s=dataset['reset_time_s'], warmup_time_s=config.get('warmup_time_s', 10.),
                      session_root=resolve(config, config.get('session_root',
                          str(Path(config['_config_root']).parent/'data/collections'))).resolve(),
                      startup_timeout_s=config.get('startup_timeout_s', 45.),
                      service_timeout_s=config.get('service_timeout_s', 15.))
        values.update({k: v for k, v in overrides.items() if v is not None})
        settings = cls(**values)
        if settings.backend not in ('real', 'mock', 'mujoco'):
            raise ValueError('backend must be real, mock or mujoco')
        if not isinstance(settings.start_runtime, bool):
            raise ValueError('runtime.start must be true or false')
        if not isinstance(settings.task, str) or not settings.task.strip():
            raise ValueError('dataset.single_task is required')
        if not isinstance(settings.operator, str) or not settings.operator.strip():
            raise ValueError('dataset.operator is required')
        if type(settings.num_episodes) is not int or settings.num_episodes <= 0:
            raise ValueError('dataset.num_episodes must be a positive integer')
        for key in ('episode_time_s', 'reset_time_s', 'warmup_time_s', 'startup_timeout_s', 'service_timeout_s'):
            value = getattr(settings, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(key+' must be finite')
            if value < 0 or (key not in ('reset_time_s', 'warmup_time_s') and value == 0):
                raise ValueError(key+' has an invalid duration')
        if not settings.runtime_config.is_file():
            raise ValueError('Runtime config does not exist: '+str(settings.runtime_config))
        return settings


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


class CollectionJournal:
    """Keep original bags/outcomes; selection is a separate annotation."""
    def __init__(self, settings):
        identity = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]
        self.folder = settings.session_root/identity
        self.folder.mkdir(parents=True, exist_ok=False)
        self.data = {'format': 'so101_collection_v1', 'session_id': identity,
                     'status': 'starting', 'start_time_ns': time.time_ns(),
                     'runtime_config': str(settings.runtime_config), 'backend': settings.backend,
                     'task': settings.task, 'operator': settings.operator,
                     'num_episodes': settings.num_episodes, 'episode_time_s': settings.episode_time_s,
                     'reset_time_s': settings.reset_time_s, 'warmup_time_s': settings.warmup_time_s,
                     'attempts': [], 'accepted_episodes': []}
        self.save()

    @property
    def accepted(self):
        return [item for item in self.data['attempts'] if item['accepted']]

    def save(self):
        self.data['accepted_episodes'] = [item['path'] for item in self.accepted]
        atomic_json(self.folder/'session.json', self.data)

    def begin(self, folder, logical_episode):
        item = {'episode_id': folder.name, 'path': str(folder.resolve()),
                'logical_episode': logical_episode, 'attempt': len(self.data['attempts'])+1,
                'accepted': False, 'status': 'recording'}
        self.data['attempts'].append(item)
        self.annotate(item)
        return item

    def annotate(self, item):
        # This sidecar never rewrites the episode outcome or MCAP timestamps.
        atomic_json(Path(item['path'])/'collection.json',
                    {'session_id': self.data['session_id'], 'session_path': str(self.folder), **item})
        self.save()

    def finish(self, item, metadata):
        item.update(accepted=bool(metadata['success']) and metadata['status'] == 'complete',
                    success=bool(metadata['success']), status='saved' if metadata['success'] else 'failed',
                    failure_reason=metadata.get('failure_reason', ''))
        self.annotate(item)

    def exclude(self, item, reason='operator requested re-recording'):
        item.update(accepted=False, status='rerecorded', selection_reason=reason)
        self.annotate(item)

    def end(self, status, reason=''):
        self.data.update(status=status, end_time_ns=time.time_ns(), reason=reason)
        self.save()


class TerminalKeys:
    """Read only the focused terminal; no global keyboard hooks or X dependency."""
    def __init__(self, stream=None):
        self.stream = stream or sys.stdin
        self.fd = None
        self.previous = None
        self.pending = b''
        self.escape_at = None

    def __enter__(self):
        if self.stream.isatty():
            self.fd = self.stream.fileno()
            self.previous = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            attrs = termios.tcgetattr(self.fd)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        return self

    def __exit__(self, *args):
        if self.previous is not None:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.previous)

    def read(self):
        if self.fd is not None and select.select([self.fd], [], [], 0)[0]:
            data = os.read(self.fd, 64)
            if not data:
                return 'quit'
            self.pending += data
        while self.pending:
            if self.pending.startswith(b'\x1b'):
                if len(self.pending) == 1 or (len(self.pending) == 2 and self.pending[1:2] in (b'[', b'O')):
                    self.escape_at = self.escape_at or time.monotonic()
                    if time.monotonic()-self.escape_at < .1:
                        return None
                    self.pending = b''
                    self.escape_at = None
                    return 'quit'
                self.escape_at = None
                if self.pending[1:2] in (b'[', b'O'):
                    final = self.pending[2:3]
                    self.pending = self.pending[3:]
                    key = {b'C': 'save', b'D': 'redo'}.get(final)
                    if key:
                        return key
                    continue
                self.pending = self.pending[1:]
                return 'quit'
            byte, self.pending = self.pending[:1], self.pending[1:]
            key = {b'\r': 'save', b'\n': 'save', b's': 'save', b'n': 'save',
                   b'r': 'redo', b'f': 'fail', b'q': 'quit', b'\x03': 'quit'}.get(byte.lower())
            if key:
                return key
        return None


class CollectionRunner:
    def __init__(self, settings, adapter, keys, journal, output=print, clock=time.monotonic):
        self.settings, self.adapter, self.keys, self.journal = settings, adapter, keys, journal
        self.output, self.clock = output, clock
        self.current = None

    def phase(self, name, duration, logical_episode):
        deadline, next_update = self.clock()+duration, 0.
        while self.clock() < deadline:
            self.adapter.pump(.02)
            self.adapter.check_health()
            now = self.clock()
            if now >= next_update:
                remaining = max(0., deadline-now)
                self.adapter.progress(name, logical_episode, len(self.journal.accepted),
                                      len(self.journal.data['attempts']), remaining, self.current)
                self.output(f'{name}  保存 {len(self.journal.accepted)}/{self.settings.num_episodes} '
                            f'試行 {len(self.journal.data["attempts"])}  残り {remaining:.1f} 秒')
                next_update = now+1.
            key = self.keys.read()
            if key:
                return key
        return 'timeout'

    def run(self):
        status, reason = 'stopped', ''
        try:
            self.journal.data['status'] = 'running'
            self.journal.save()
            self.adapter.enter_teleop()
            if self.phase('WARMUP', self.settings.warmup_time_s, 1) == 'quit':
                return
            while len(self.journal.accepted) < self.settings.num_episodes:
                logical = len(self.journal.accepted)+1
                folder = self.adapter.start_episode(self.settings.task, self.settings.operator)
                self.current = self.journal.begin(folder, logical)
                self.output('記録開始: '+str(folder))
                key = self.phase('RECORDING', self.settings.episode_time_s, logical)
                success = key in ('timeout', 'save')
                why = {'redo': 'Operator requested re-recording', 'fail': 'Operator marked task failure',
                       'quit': 'Collection interrupted by operator'}.get(key, '')
                metadata = self.adapter.stop_episode(success, why)
                self.journal.finish(self.current, metadata)
                if key == 'redo':
                    self.journal.exclude(self.current)
                self.output(f'記録終了: {self.current["episode_id"]}  学習用に選択={self.current["accepted"]}')
                if key == 'quit':
                    break
                # The final episode also gets a review/reset window for re-recording.
                key = self.phase('RESET', self.settings.reset_time_s, logical)
                if key in ('redo', 'fail'):
                    self.journal.exclude(self.current)
                    self.output('直前の試行を学習対象から外し、同じ本数位置でやり直します。')
                    key = self.phase('RESET', self.settings.reset_time_s, logical)
                if key == 'quit':
                    break
            if len(self.journal.accepted) == self.settings.num_episodes:
                status = 'complete'
        except BaseException as error:
            reason = str(error) or type(error).__name__
            status = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'error'
            raise
        finally:
            try:
                metadata = self.adapter.cleanup(reason or 'Collection interrupted')
                if metadata is not None:
                    if self.current is None or self.current['episode_id'] != metadata['episode_id']:
                        self.current = self.journal.begin(Path(metadata['bag_uri']).parent,
                                                          len(self.journal.accepted)+1)
                    if self.current['status'] == 'recording':
                        self.journal.finish(self.current, metadata)
            except Exception as error:
                status, reason = 'error', 'Cleanup failed: '+str(error)
                raise
            finally:
                self.journal.end(status, reason)
                self.adapter.progress(status.upper(), len(self.journal.accepted), len(self.journal.accepted),
                                      len(self.journal.data['attempts']), 0., self.current)

"""Exercise evaluation deadlines, reset pause and interrupted recording without hardware."""
import pytest
from so101_core.benchmark import BenchmarkRunner


class Clock:
    def __init__(self): self.now = 0.
    def __call__(self): return self.now


class Adapter:
    def __init__(self, clock):
        self.clock = clock
        self.has_attempt = False
        self.phase = None
        self.phase_start = 0.
        self.events, self.results, self.selections = [], [], []
        self.pumps = 0
        self.fail_health = False
    def ready(self): pass
    def pump(self, timeout):
        self.clock.now += .02
        self.pumps += 1
        assert self.pumps < 10000, 'Runner stalled'
    def check_health(self, expected):
        if self.fail_health and self.has_attempt and self.clock()-self.started > .1:
            self.fail_health = False
            raise RuntimeError('camera disconnected')
    def select(self, item): self.selections.append(item)
    def progress(self, phase, trial, remaining, paused):
        if self.phase != phase:
            self.phase_start = self.clock()
        self.phase = phase
    def event(self, kind, **values): self.events.append((kind, self.clock(), values))
    def begin(self, item):
        self.has_attempt = True
        self.started = self.clock()
        return {'start_monotonic_s': self.started}
    def finish(self, outcome, reason, ended):
        self.has_attempt = False
        row = {'outcome': outcome, 'duration_s': ended-self.started, 'reason': reason,
               'recording': 'synthetic-recording'}
        self.results.append(row)
        return row


class Keys:
    def __init__(self, adapter, script): self.adapter, self.script = adapter, list(script)
    def flush(self): pass
    def read(self):
        if self.script:
            phase, offset, key = self.script[0]
            if self.adapter.phase == phase and self.adapter.clock()-self.adapter.phase_start >= offset:
                self.script.pop(0)
                return key


class Journal:
    def __init__(self): self.rows, self.phases = [], []
    def phase(self, phase, item, remaining, paused): self.phases.append((phase, remaining, paused))
    def begin(self, item): pass
    def started(self, item, start): pass
    def finish(self, item, data): self.rows.append((item, data))
    def end(self, status, reason): self.status, self.reason = status, reason


def make_runner(script, count=2, reset=1., limit=1.):
    clock = Clock(); adapter = Adapter(clock); journal = Journal()
    keys = Keys(adapter, script)
    trials = [{'trial_id': n, 'condition': str(n)} for n in range(1, count+1)]
    runner = BenchmarkRunner(trials, adapter, keys, journal, reset, limit, lambda _: None, clock)
    return runner, adapter, journal, clock


def test_success_failure_reset_pause_freezes_countdown():
    runner, adapter, journal, clock = make_runner([
        ('READY', 0, 'save'), ('RUNNING', .2, 'save'),
        ('RESET', .2, 'pause'), ('RESET', .9, 'pause'), ('RUNNING', .2, 'fail')])
    runner.run()
    assert journal.status == 'complete'
    assert [r['outcome'] for r in adapter.results] == ['success', 'failure']
    assert any(phase == 'RESET' and paused for phase, _, paused in journal.phases)
    assert [e[0] for e in adapter.events] == ['reset_started', 'reset_pause', 'reset_resume']
    assert clock() >= 2.1  # .2 + (1 reset + .7 paused) + .2, with pump overhead
    assert len(adapter.selections) == 2


def test_timeout_is_failure_not_collection_style_success():
    runner, adapter, journal, _ = make_runner([('READY', 0, 'save')], count=1)
    runner.run()
    assert adapter.results[0]['outcome'] == 'failure'
    assert adapter.results[0]['duration_s'] >= 1.
    assert journal.status == 'complete'


def test_pause_during_motion_aborts_attempt_and_waits_for_explicit_resume():
    runner, adapter, journal, _ = make_runner([
        ('READY', 0, 'save'), ('RUNNING', .1, 'pause'),
        ('RESET', 2., 'pause'), ('RUNNING', .1, 'save')], reset=.4)
    runner.run()
    assert [r['outcome'] for r in adapter.results] == ['aborted', 'success']
    assert any(phase == 'RESET' and paused for phase, _, paused in journal.phases)
    assert journal.status == 'complete'


def test_quit_during_motion_saves_abort_and_does_not_run_next_trial():
    runner, adapter, journal, _ = make_runner([('READY', 0, 'save'), ('RUNNING', .1, 'quit')])
    runner.run()
    assert adapter.results[0]['outcome'] == 'aborted'
    assert len(adapter.selections) == 1 and journal.status == 'stopped'


def test_health_failure_closes_and_records_current_attempt():
    runner, adapter, journal, _ = make_runner([('READY', 0, 'save')])
    adapter.fail_health = True
    with pytest.raises(RuntimeError, match='camera disconnected'): runner.run()
    assert journal.status == 'error' and not adapter.has_attempt
    assert adapter.results[0]['outcome'] == 'aborted'
    assert 'camera disconnected' in journal.rows[0][1]['reason']


def test_quit_before_start_does_not_fabricate_result():
    runner, adapter, journal, _ = make_runner([('READY', 0, 'quit')])
    runner.run()
    assert journal.rows == [] and not adapter.has_attempt
    assert journal.status == 'stopped'

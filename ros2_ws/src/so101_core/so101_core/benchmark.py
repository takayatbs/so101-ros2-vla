"""Interactive evaluation state machine, independent of ROS and model libraries."""
import time


class BenchmarkRunner:
    def __init__(self, trials, adapter, keys, journal, reset_time_s=15., max_duration_s=30.,
                 output=print, clock=time.monotonic):
        self.trials, self.adapter, self.keys, self.journal = trials, adapter, keys, journal
        self.reset_time_s, self.max_duration_s = reset_time_s, max_duration_s
        self.output, self.clock = output, clock
        self.current = None

    def display(self, phase, remaining=0., paused=False):
        self.journal.phase(phase, self.current, remaining, paused)
        self.adapter.progress(phase, self.current, remaining, paused)
        item = self.current or {}
        self.output(f'{phase}  試行 {item.get("trial_id", "-")}/{len(self.trials)} '
                    f'{item.get("condition", "")}  残り {remaining:.1f}秒'
                    + ('  一時停止中（Space/pで再開）' if paused else ''))

    def reset(self, paused=False):
        remaining = self.reset_time_s
        previous, update = self.clock(), -1.
        self.keys.flush()
        self.adapter.event('reset_started', remaining_s=remaining, paused=paused)
        while remaining > 0 or paused:
            self.adapter.pump(.02)
            self.adapter.check_health('IDLE')
            now = self.clock()
            if not paused:
                remaining = max(0., remaining-(now-previous))
            previous = now
            key = self.keys.read()
            if key == 'quit':
                return False
            if key == 'pause':
                paused = not paused
                update = -1.
                self.adapter.event('reset_pause' if paused else 'reset_resume', remaining_s=remaining)
            if now >= update:
                self.display('RESET', remaining, paused)
                update = now+1.
        return True

    def finish(self, outcome, reason='', stopped_at=None):
        ended = self.clock() if stopped_at is None else stopped_at
        data = self.adapter.finish(outcome, reason, ended)
        # Persist before propagating recording/stop failures. Never silently lose an attempt.
        self.journal.finish(self.current, data)
        self.output(f'保存: 試行{self.current["trial_id"]} {data["outcome"]} '
                    f'{data["duration_s"]:.2f}秒 / {data.get("recording", "")}')
        if data.get('error'):
            raise RuntimeError(data['error'])

    def run(self):
        status, reason = 'stopped', ''
        try:
            self.adapter.ready()
            if not self.trials:
                status = 'complete'
                return
            for index, item in enumerate(self.trials):
                self.current = item
                self.display('PREPARING')
                self.adapter.select(item)
                self.keys.flush()
                if index == 0:
                    self.display('READY')
                    self.output('初期配置を確認してEnter/→/sで開始。開始後は戻し時間を挟んで自動的に次へ進みます。')
                    while True:
                        self.adapter.pump(.02)
                        self.adapter.check_health('IDLE')
                        key = self.keys.read()
                        if key == 'quit':
                            return
                        if key == 'save':
                            break
                self.keys.flush()
                self.journal.begin(item)
                start = self.adapter.begin(item)
                self.journal.started(item, start)
                self.display('RUNNING', self.max_duration_s)
                update = self.clock()+1.
                action, stopped_at = None, None
                while action is None:
                    self.adapter.pump(.01)
                    self.adapter.check_health('AUTONOMOUS')
                    now = self.clock()
                    elapsed = now-start['start_monotonic_s']
                    key = self.keys.read()
                    # The deadline wins over late success keys.
                    if elapsed >= self.max_duration_s:
                        action = 'timeout'
                    elif key in ('save', 'fail', 'abort', 'quit', 'pause'):
                        action = key
                    if action:
                        stopped_at = now
                    elif now >= update:
                        self.display('RUNNING', self.max_duration_s-elapsed)
                        update = now+1.
                outcome = 'success' if action == 'save' else 'failure' if action in ('fail', 'timeout') else 'aborted'
                why = {'save': '', 'fail': 'Operator marked task failure', 'timeout': 'Time limit reached',
                       'pause': 'Operator paused during execution', 'abort': 'Operator aborted attempt',
                       'quit': 'Operator ended session'}.get(action, '')
                self.finish(outcome, why, stopped_at)
                if action == 'quit':
                    return
                if index == len(self.trials)-1:
                    status = 'complete'
                    break
                if not self.reset(paused=action == 'pause'):
                    return
        except BaseException as error:
            reason = str(error) or type(error).__name__
            status = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'error'
            if self.adapter.has_attempt:
                try:
                    self.finish('aborted', reason)
                except Exception as cleanup_error:
                    reason += '; cleanup: '+str(cleanup_error)
            raise
        finally:
            self.journal.end(status, reason)
            self.display(status.upper())

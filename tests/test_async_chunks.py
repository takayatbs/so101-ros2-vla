"""Deterministic wall-clock tests; no model, GPU, ROS, or robot is needed."""
from concurrent.futures import Future
from types import SimpleNamespace

import numpy as np
import pytest

from so101_core.async_chunks import AsyncChunkScheduler, smooth_action_chunk


class TimedExecutor:
    def __init__(self, clock, latencies):
        self.clock = clock
        self.latencies = iter(latencies)
        self.pending = None
        self.submissions = []

    def submit(self, fn, observation, previous, delay):
        latency = next(self.latencies)
        self.submissions.append((self.clock[0], observation, previous, delay))
        future = Future()
        chunk = observation['observation.state'][None] + np.arange(1, 31)[:, None]
        self.pending = (future, chunk, latency, self.clock[0] + latency)
        return future

    def complete(self):
        if self.pending is not None:
            future, chunk, latency, finished = self.pending
            if self.clock[0] >= finished:
                future.set_result((chunk, latency, finished))
                self.pending = None

    def shutdown(self, **kwargs):
        pass


def scheduler(latencies, method='vlash', smoothing='none'):
    clock = [0.]
    policy = SimpleNamespace(config={
        'objective': 'standard' if method in ('rtc', 'standard') else method,
        'chunk_size': 30, 'joint_names': ['q'],
        'max_delay_steps': 15, 'fps': 30,
    })
    engine = AsyncChunkScheduler(policy, method, 30, 10, 15, .6,
                                 clock=lambda: clock[0], smoothing=smoothing)
    engine.executor.shutdown()
    engine.executor = TimedExecutor(clock, latencies)
    return engine, clock, {'observation.state': np.zeros(1)}


def test_vlash_runs_five_minutes_with_latency_variation_and_timer_jitter():
    # A long run at 395 ms removes the initial 15-step estimate from the old
    # rolling window. A subsequent 425 ms inference still fits the trained
    # 500 ms budget and must not cause the scheduler to discard and retry it.
    latencies = iter([.395] * 20 + [.425, .395, .455, .410] * 3000)
    engine, clock, observation = scheduler(latencies)
    try:
        for tick in range(9001):
            clock[0] = tick / 30 + (.008 if tick % 7 == 0 else 0)
            engine.executor.complete()
            action = engine.next(observation)
            assert action.shape == (1,) and np.isfinite(action).all()
            if engine.chunk is not None:
                # This synthetic policy describes a single time-indexed ramp.
                # Replanning must preserve its absolute timeline across chunks.
                assert action[0] == engine.last_step + 1
        assert len(engine.executor.submissions) > 300
        assert engine.missed == 0
    finally:
        engine.close()


def test_bootstrap_at_trained_latency_keeps_enough_coverage_to_start_next_job():
    engine, clock, observation = scheduler(iter([.5] * 10))
    try:
        engine.next(observation)
        clock[0] = .5
        engine.executor.complete()
        action = engine.next(observation)
        assert np.isfinite(action).all()
        assert len(engine.executor.submissions) == 2
    finally:
        engine.close()


@pytest.mark.parametrize('smoothing', ['none', 'three_point'])
def test_actual_latency_beyond_training_support_still_stops(smoothing):
    engine, clock, observation = scheduler(iter([.395, .535]), smoothing=smoothing)
    try:
        engine.next(observation)
        clock[0] = .4
        engine.executor.complete()
        engine.next(observation)
        clock[0] = .95
        engine.executor.complete()
        with pytest.raises(ValueError, match='trained delay'):
            engine.next(observation)
    finally:
        engine.close()


@pytest.mark.parametrize('method', ['standard', 'rtc', 'training_time_rtc', 'vlash'])
def test_supported_methods_cover_back_to_back_maximum_latency_jobs(method):
    engine, clock, observation = scheduler(iter([.5] * 100), method)
    try:
        for tick in range(901):
            clock[0] = tick / 30
            engine.executor.complete()
            assert np.isfinite(engine.next(observation)).all()
        assert len(engine.executor.submissions) > 20
        assert engine.missed == 0
    finally:
        engine.close()


@pytest.mark.parametrize('smoothing', ['none', 'three_point'])
def test_unfinished_inference_deadline_still_stops(smoothing):
    engine, clock, observation = scheduler(iter([1.]), smoothing=smoothing)
    try:
        engine.next(observation)
        clock[0] = .61
        with pytest.raises(ValueError, match='deadline'):
            engine.next(observation)
    finally:
        engine.close()


def test_three_point_filter_reduces_arm_impulse_without_changing_gripper_or_input():
    raw = np.array([[0, 0], [0, 0], [4, 1], [0, 1], [0, 1]], dtype=np.float32)
    original = raw.copy()
    filtered = smooth_action_chunk(raw, [0])
    np.testing.assert_allclose(filtered[:, 0], [0, 1, 2, 1, 0])
    np.testing.assert_array_equal(filtered[:, 1], original[:, 1])
    np.testing.assert_array_equal(raw, original)
    assert not np.shares_memory(filtered, raw)


def test_smoothed_committed_goals_are_used_for_vlash_future_state():
    engine, clock, observation = scheduler(iter([.395] * 10), smoothing='three_point')
    try:
        engine.next(observation)
        future, _, latency, finished = engine.executor.pending
        raw = np.zeros((30, 1), dtype=np.float32)
        raw[26] = 4
        future.set_result((raw, latency, finished))
        engine.executor.pending = None
        clock[0] = .4
        engine.next(observation)
        assert engine.chunk[26, 0] == 2
        assert engine.executor.submissions[-1][1]['observation.state'][0] == 2
        assert raw[26, 0] == 4
        assert engine.filtered_roughness_rad < engine.raw_roughness_rad
    finally:
        engine.close()


@pytest.mark.parametrize('value', [np.nan, np.inf])
def test_smoothing_does_not_hide_invalid_raw_predictions(value):
    engine, clock, observation = scheduler(iter([.395]), smoothing='three_point')
    try:
        engine.next(observation)
        future, raw, latency, finished = engine.executor.pending
        raw[15, 0] = value
        future.set_result((raw, latency, finished))
        engine.executor.pending = None
        clock[0] = .4
        with pytest.raises(ValueError, match='Invalid asynchronous'):
            engine.next(observation)
    finally:
        engine.close()


def test_unknown_smoothing_is_rejected():
    policy = SimpleNamespace(config={'objective': 'standard', 'chunk_size': 30,
        'joint_names': ['q'], 'max_delay_steps': 15, 'fps': 30})
    with pytest.raises(ValueError, match='Unknown action smoothing'):
        AsyncChunkScheduler(policy, 'rtc', 30, 10, 15, .6, smoothing='unknown')


@pytest.mark.parametrize('method', ['rtc', 'training_time_rtc'])
def test_rtc_smoothing_preserves_the_committed_prefix(method):
    engine, clock, observation = scheduler(iter([.395] * 10), method, 'three_point')
    try:
        engine.next(observation)
        clock[0] = .4
        engine.executor.complete()
        engine.next(observation)
        future, _, latency, finished = engine.executor.pending
        raw = np.zeros((30, 1), dtype=np.float32)
        raw[:15, 0] = np.arange(15)
        raw[20, 0] = 4
        future.set_result((raw, latency, finished))
        engine.executor.pending = None
        clock[0] = .9
        engine.next(observation)
        np.testing.assert_array_equal(engine.chunk[:15], raw[:15])
        assert engine.chunk[20, 0] == 2
    finally:
        engine.close()


def test_a2c2_uses_raw_base_predictions_then_filters_final_commands():
    class Correction:
        config = {'max_delay_steps': 15}
        def __init__(self): self.bases = []
        def correct(self, obs, base, position, delay):
            self.bases.append(base.copy())
            return base + obs['observation.state']
    engine, clock, observation = scheduler(iter([.395] * 10), smoothing='three_point')
    engine.correction = Correction()
    try:
        engine.next(observation)
        future, _, latency, finished = engine.executor.pending
        raw = np.zeros((30, 1), dtype=np.float32); raw[26] = 4
        future.set_result((raw, latency, finished)); engine.executor.pending = None
        clock[0] = .4; observation['observation.state'] = np.array([4.])
        np.testing.assert_allclose(engine.next(observation), [1.])
        assert engine.chunk[26, 0] == 4
        np.testing.assert_array_equal(engine.correction.bases[-1], [0.])
        clock[0] = 13/30; observation['observation.state'] = np.array([0.])
        np.testing.assert_allclose(engine.next(observation), [2.])
        engine.reset()
        assert not engine.smoother.inputs
    finally:
        engine.close()


@pytest.mark.parametrize('smoothing', ['none', 'three_point'])
def test_exhausted_chunk_still_stops(smoothing):
    engine, clock, observation = scheduler(iter([.395] * 10), smoothing=smoothing)
    try:
        engine.next(observation)
        clock[0] = .4
        engine.executor.complete()
        engine.next(observation)
        clock[0] = 1.01
        with pytest.raises(ValueError, match='trajectory|exhausted|deadline'):
            engine.next(observation)
    finally:
        engine.close()


@pytest.mark.parametrize('method', ['standard', 'rtc', 'training_time_rtc', 'vlash'])
@pytest.mark.parametrize('smoothing', ['none', 'three_point'])
def test_refill_uses_available_committed_window_under_timer_jitter(method, smoothing):
    engine, clock, observation = scheduler(iter([.395, .425, .410] * 1000), method, smoothing)
    clipped = False
    try:
        for tick in range(901):
            clock[0] = tick/30 + (.008 if tick % 7 == 0 else 0)
            engine.executor.complete()
            action = engine.next(observation)
            assert np.isfinite(action).all()
            if engine.job is not None:
                clipped |= engine.job['has_previous'] and engine.job['delay'] < engine.max_delay
        assert engine.missed == 0
        if method != 'vlash':
            assert clipped
    finally:
        engine.close()


def test_clipped_reservation_still_rejects_a_job_longer_than_remaining_trajectory():
    engine, clock, observation = scheduler(iter([.395] * 100), 'standard')
    try:
        for tick in range(100):
            clock[0] = tick/30 + (.008 if tick % 7 == 0 else 0)
            engine.executor.complete()
            engine.next(observation)
            if engine.job is not None and engine.job['has_previous'] and engine.job['delay'] < engine.max_delay:
                future, chunk, _, _ = engine.executor.pending
                # Inside the configured maximum, outside the remaining plan.
                latency = (engine.job['delay']+.5)/engine.fps
                assert latency < engine.max_delay/engine.fps
                finished = engine.job['requested_at']+latency
                future.set_result((chunk, latency, finished))
                engine.executor.pending = None
                clock[0] = finished
                with pytest.raises(ValueError, match='available trajectory window'):
                    engine.next(observation)
                assert engine.missed == 1
                return
        pytest.fail('Expected a clipped reservation under jitter')
    finally:
        engine.close()

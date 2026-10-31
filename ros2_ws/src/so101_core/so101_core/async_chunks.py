"""Wall-clock-indexed asynchronous action chunks; stale generations never commit."""
from concurrent.futures import ThreadPoolExecutor
import math
import time
import numpy as np
from .action_smoothing import ActionSmoother, smooth_action_chunk as smooth_action_chunk


class AsyncChunkScheduler:
    def __init__(self, policy, method, fps, execution_horizon, max_delay_steps, max_inference_s,
                 correction=None, clock=time.monotonic, smoothing='none'):
        if method not in ('rtc','training_time_rtc','vlash','standard'):
            raise ValueError('Unknown scheduler method')
        objective = policy.config['objective']
        required = {'rtc':'standard','training_time_rtc':'training_time_rtc','vlash':'vlash'}
        if method in required and objective != required[method]:
            raise ValueError(f'{method} requires a {required[method]} training checkpoint')
        self.smoother = ActionSmoother(smoothing, policy.config['joint_names'])
        h = policy.config['chunk_size']
        if not 0 < execution_horizon < h or not 0 <= max_delay_steps < h-execution_horizon:
            raise ValueError('Chunk must cover execution_horizon + max_delay_steps + 1')
        if (method in ('training_time_rtc','vlash') and max_delay_steps > policy.config['max_delay_steps']) or fps != policy.config['fps']:
            raise ValueError('Runtime delay/FPS differs from training support')
        if correction is not None and max_delay_steps > correction.config['max_delay_steps']:
            raise ValueError('Runtime delay exceeds A2C2 cache training range')
        self.policy,self.method,self.fps = policy,method,fps
        self.horizon,self.max_delay,self.max_inference = execution_horizon,max_delay_steps,max_inference_s
        self.correction,self.clock = correction,clock
        self.smoothing = smoothing
        self.arm_indices = self.smoother.arm_indices
        self.executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='chunk-inference')
        self.future,self.job = None,None
        self.epoch,self.missed,self.latency_s = 0,0,0.
        self.reset()

    def reset(self):
        self.epoch += 1
        self.origin = None
        self.chunk,self.start,self.last_step = None,0,-1
        self.raw_roughness_rad = self.filtered_roughness_rad = 0.
        self.smoother.reset()

    def _infer(self, observation, previous, delay):
        began = self.clock()
        result = self.policy.predict(observation,method=self.method,previous=previous,delay=delay)
        return result,self.clock()-began,self.clock()

    def _submit(self, observation, step, now):
        # Reserve the supported latency budget, not a rolling estimate of
        # recent fast jobs. An underestimated job cannot simply be discarded
        # and retried: the old chunk has already spent that inference time.
        delay = self.max_delay
        previous,offset = None,0
        obs = dict(observation)
        if self.chunk is not None:
            previous = self.chunk[max(0,step-self.start):].copy()
            # Exactly `delay` old goals cover [step, step + delay). The new
            # chunk takes over at step + delay, so equality is sufficient.
            if not len(previous) or len(previous) < delay:
                raise ValueError('No committed trajectory left to cover inference delay')
            if self.method == 'vlash':
                # Absolute joint-position actions: last committed goal estimates
                # the execution-time state. Never read a future measured state.
                obs['observation.state'] = previous[max(0,delay-1)].copy() if delay else obs['observation.state']
                offset = delay
                previous = None
        self.job = {'epoch':self.epoch,'request_step':step,'offset':offset,'delay':delay,
                    'requested_at':now,'has_previous':self.chunk is not None}
        prefix_delay = delay if previous is not None and self.method in ('rtc','training_time_rtc') else 0
        self.future = self.executor.submit(self._infer,obs,previous,prefix_delay)

    def next(self, observation):
        now = self.clock()
        if self.origin is None:self.origin = now
        step = max(self.last_step,int(math.floor((now-self.origin)*self.fps+1e-7)))
        if self.future is not None and self.future.done() and self.job['epoch'] != self.epoch:
            # Even an exception from an abandoned generation belongs to the old mode.
            self.future,self.job=None,None
        if self.future is not None:
            if self.future.done():
                chunk,latency,finished = self.future.result()
                job = self.job
                elapsed = max(0,math.ceil((finished-job['requested_at'])*self.fps-1e-7))
                if job['epoch'] != self.epoch:
                    self.future,self.job = None,None
                elif finished-job['requested_at'] > self.max_inference:
                    raise ValueError('Inference exceeded configured maximum latency')
                elif job['has_previous'] and elapsed > job['delay']:
                    self.missed += 1
                    raise ValueError('Inference latency exceeds the trained delay range')
                elif step >= job['request_step']+(job['delay'] if job['has_previous'] else elapsed):
                    chunk = np.asarray(chunk)
                    expected = (self.policy.config['chunk_size'],len(self.policy.config['joint_names']))
                    if chunk.shape != expected or not np.isfinite(chunk).all():
                        raise ValueError('Invalid asynchronous action chunk')
                    if self.correction is None:
                        # Preserve RTC's committed past. VLASH's future-state
                        # estimate then uses the same filtered plan we execute.
                        protected = 0 if self.method == 'vlash' else min(
                            len(chunk), max(0, step-job['request_step']))
                        chunk = self.smoother.chunk(chunk, protected)
                        self.raw_roughness_rad = self.smoother.raw_roughness_rad
                        self.filtered_roughness_rad = self.smoother.filtered_roughness_rad
                    self.chunk,self.start = chunk,job['request_step']+job['offset']
                    self.latency_s = latency
                    self.future,self.job = None,None
            elif now-self.job['requested_at'] > self.max_inference:
                raise ValueError('Inference deadline exceeded')
        if self.future is None and (self.chunk is None or step-self.start >= self.horizon):
            self._submit(observation,step,now)
        self.last_step = step
        if self.chunk is None:
            # Bounded bootstrap while the first chunk is being generated.
            action = np.asarray(observation['observation.state']).copy()
            return self.smoother.action(action) if self.correction is not None else action
        index = step-self.start
        if not 0 <= index < len(self.chunk):
            raise ValueError('Action chunk exhausted; refusing to repeat a stale command')
        base = self.chunk[index].copy()
        if self.correction is None:
            return base
        # Keep the correction's raw base-input distribution intact. Future
        # corrected goals are unavailable, so smooth final commands causally.
        action = self.correction.correct(observation,base,index,math.ceil(self.latency_s*self.fps))
        result = self.smoother.action(action)
        self.raw_roughness_rad = self.smoother.raw_roughness_rad
        self.filtered_roughness_rad = self.smoother.filtered_roughness_rad
        return result

    def close(self):
        self.epoch += 1
        self.executor.shutdown(wait=True,cancel_futures=True)

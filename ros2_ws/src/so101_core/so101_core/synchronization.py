from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import numpy as np


@dataclass
class Sample:
    time: float
    value: object
    timestamp_ns: int = None
    message_id: int = -1
    topic: str = ""
    receive_ns: int = None
    frame_id: str = ""
    velocity: object = None
    effort: object = None
    capture_sequence: int = None

    def __post_init__(self):
        if self.timestamp_ns is None:
            self.timestamp_ns = round(self.time*1_000_000_000)


class Timeline:
    def __init__(self, samples):
        self.samples = list(samples)
        self.times = [s.time for s in self.samples]
        if not self.samples:
            raise ValueError("Empty topic timeline")
        self.times_ns = [s.timestamp_ns for s in self.samples]
        if not np.isfinite(self.times).all() or any(b <= a for a, b in zip(self.times_ns, self.times_ns[1:])):
            raise ValueError("Source timestamps must be finite and strictly increasing; duplicates/reversals are not sorted away")

    def nearest(self, time, tolerance):
        i = bisect_left(self.times, time)
        choices = self.samples[max(0, i-1):min(len(self.samples), i+1)]
        best = min(choices, key=lambda s: abs(s.time-time))
        if abs(best.time-time) > tolerance:
            raise ValueError("Image synchronization tolerance exceeded")
        return best

    def previous(self, time, tolerance):
        i = bisect_right(self.times, time)-1
        if i < 0 or time-self.times[i] > tolerance:
            raise ValueError("No recent actually applied action")
        return self.samples[i]

    def interpolate(self, time, tolerance):
        i = bisect_left(self.times, time)
        if i < len(self.times) and abs(self.times[i]-time) < 1e-9:
            return np.asarray(self.samples[i].value)
        if i == 0 or i == len(self.times):
            raise ValueError("State interpolation requires bracketing samples")
        a, b = self.samples[i-1], self.samples[i]
        if max(time-a.time, b.time-time) > tolerance:
            raise ValueError("State synchronization tolerance exceeded")
        alpha = (time-a.time)/(b.time-a.time)
        return (1-alpha)*np.asarray(a.value)+alpha*np.asarray(b.value)


def synchronized_frames(states, actions, cameras, fps, tolerance, action_tolerance):
    """Strict uniform timeline: do not silently compress time by dropping gaps."""
    topics = [states, actions, *cameras.values()]
    start = max(t.times[0] for t in topics)
    end = min(t.times[-1] for t in topics)
    if end <= start:
        raise ValueError("Topic timelines do not overlap")
    for i in range(int(np.floor((end-start)*fps))+1):
        time = start+i/fps
        images = {name: timeline.nearest(time, tolerance) for name, timeline in cameras.items()}
        image_times = [sample.time for sample in images.values()]
        if max(image_times)-min(image_times) > tolerance:
            raise ValueError("Camera-camera synchronization tolerance exceeded")
        for image_time in image_times:
            states.nearest(image_time, tolerance)
        yield time, states.interpolate(time, tolerance), actions.previous(time, action_tolerance).value, {
            name: sample.value for name, sample in images.items()}


def select_sample(timeline, target_ns, method="previous"):
    if method == "previous":
        index = bisect_right(timeline.times_ns, target_ns)-1
        return timeline.samples[index] if index >= 0 else None
    if method == "exact":
        index = bisect_left(timeline.times_ns, target_ns)
        return timeline.samples[index] if index < len(timeline.samples) and timeline.times_ns[index] == target_ns else None
    if method == "nearest":
        index = bisect_left(timeline.times_ns, target_ns)
        choices = timeline.samples[max(0,index-1):min(len(timeline.samples),index+1)]
        return min(choices, key=lambda s: (abs(s.timestamp_ns-target_ns), s.timestamp_ns))
    raise ValueError("image_selection must be previous, nearest or exact")


def alignment_settings(spec):
    settings = {"image_selection": "previous", "warning_s": .02, "reject_s": .05,
                "action_max_age_s": spec.get("action_tolerance_s", .05),
                "camera_skew_s": .05, "action_offset_s": 0., "hardware_synchronized": False}
    settings.update(spec.get("synchronization", {}))
    if not 0 <= settings["warning_s"] <= settings["reject_s"] or settings["reject_s"] <= 0:
        raise ValueError("Require 0 <= warning_s <= reject_s")
    if settings["action_max_age_s"] <= 0 or settings["camera_skew_s"] < 0:
        raise ValueError("Invalid synchronization age/skew")
    if settings["image_selection"] == "exact" and not settings["hardware_synchronized"]:
        raise ValueError("Exact matching requires declared hardware synchronization")
    if settings["image_selection"] not in ("previous", "nearest", "exact"):
        raise ValueError("Unknown image selection")
    if not np.isfinite([settings[k] for k in ("warning_s", "reject_s", "action_max_age_s", "camera_skew_s", "action_offset_s")]).all():
        raise ValueError("Synchronization settings must be finite")
    return settings


def aligned_samples(states, actions, cameras, fps, settings, inputs=None, required_inputs=()):
    """Retain every target row and its source identities, even rejected rows.

    State and submitted action use a causal zero-order hold. Invalid rows are
    masked, never removed from the clock; the loader must not chunk across them.
    """
    from fractions import Fraction
    if not np.isfinite(fps) or fps <= 0:
        raise ValueError("FPS must be positive")
    rate = Fraction(str(fps))
    inputs = inputs or {}
    if not set(required_inputs) <= set(inputs):
        raise ValueError('Missing required operator input timeline')
    offset = round(settings["action_offset_s"]*1e9)
    start = max(states.times_ns[0], actions.times_ns[0]-offset, *(t.times_ns[0] for t in cameras.values()), *(inputs[n].times_ns[0] for n in required_inputs))
    end = min(states.times_ns[-1], actions.times_ns[-1]-offset, *(t.times_ns[-1] for t in cameras.values()), *(inputs[n].times_ns[-1] for n in required_inputs))
    if end <= start:
        raise ValueError("Topic timelines do not overlap")
    count = (end-start)*rate.numerator//(1_000_000_000*rate.denominator)+1
    for index in range(count):
        target = start+round(Fraction(index*1_000_000_000,1)/rate)
        state = select_sample(states, target)
        action = select_sample(actions, target+offset)
        images = {n: select_sample(t, target, settings["image_selection"]) for n,t in cameras.items()}
        operator = {n:select_sample(t,target) for n,t in inputs.items()}
        selected = {"state": state, "action": action, **{"image/"+n:s for n,s in images.items()}, **{'input/'+n:s for n,s in operator.items()}}
        errors = {n: (s.timestamp_ns-target)/1e9 if s else None for n,s in selected.items()}
        reasons, warnings = [], []
        for name, sample in selected.items():
            if sample is None:
                if not name.startswith('input/') or name[6:] in required_inputs:
                    reasons.append(name+":missing")
                continue
            query = target+offset if name == "action" else target
            error = abs(sample.timestamp_ns-query)/1e9
            limit = settings["action_max_age_s"] if name == "action" else settings["reject_s"]
            if error > limit+1e-9:
                (warnings if name.startswith('input/') and name[6:] not in required_inputs else reasons).append(name+":time_error")
            elif error > settings["warning_s"]+1e-9:
                warnings.append(name+":time_error")
        image_times = [s.timestamp_ns for s in images.values() if s is not None]
        if len(image_times)>1 and max(image_times)-min(image_times) > round(settings["camera_skew_s"]*1e9):
            reasons.append("camera_skew")
        yield {"index": index, "target_ns": target, "state": state, "action": action,
               "action_query_ns": target+offset, "images": images, "errors": errors,
               "inputs":operator,
               "valid": not reasons, "warnings": warnings, "reasons": reasons}


def timeline_report(timeline, expected_fps=None):
    deltas = np.diff(timeline.times_ns)/1e9
    duration = (timeline.times_ns[-1]-timeline.times_ns[0])/1e9
    median = float(np.median(deltas)) if len(deltas) else None
    return {"messages": len(timeline.samples), "start_ns": timeline.times_ns[0], "end_ns": timeline.times_ns[-1],
            "observed_hz": (len(timeline.samples)-1)/duration if duration else None,
            "median_interval_s": median, "max_interval_s": float(max(deltas)) if len(deltas) else None,
            "long_gap_count": int(np.sum(deltas>median*1.5)) if median else 0,
            "configured_fps": expected_fps, "timestamp_reversals": 0, "duplicate_timestamps": 0}

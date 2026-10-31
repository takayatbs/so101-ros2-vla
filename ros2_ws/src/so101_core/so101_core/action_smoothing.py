"""Shared inference postprocessing, independent of model and ROS libraries."""
from collections import deque

import numpy as np


METHODS = ('none', 'three_point')


def smooth_action_chunk(chunk, arm_indices, protected_prefix=0):
    """Filter predicted goals without modifying gripper or committed prefix."""
    result = np.array(chunk, dtype=np.result_type(chunk.dtype, np.float32), copy=True)
    previous = np.concatenate((result[:1], result[:-1]), axis=0)
    following = np.concatenate((result[1:], result[-1:]), axis=0)
    result[protected_prefix:, arm_indices] = (
        .25*previous[protected_prefix:, arm_indices] + .5*result[protected_prefix:, arm_indices]
        + .25*following[protected_prefix:, arm_indices])
    return result


class ActionSmoother:
    def __init__(self, method='none', joint_names=None):
        if method not in METHODS:
            raise ValueError('Unknown action smoothing method')
        if method != 'none' and not joint_names:
            raise ValueError('Action smoothing requires joint names to preserve the gripper')
        self.method = method
        self.joint_names = None if joint_names is None else list(joint_names)
        self.arm_indices = [] if joint_names is None else [
            i for i, name in enumerate(joint_names) if name != 'gripper']
        self.reset()

    def reset(self):
        self.raw_roughness_rad = self.filtered_roughness_rad = 0.
        self.inputs = deque(maxlen=3)
        self.outputs = deque(maxlen=3)

    def _validate(self, value, ndim):
        value = np.asarray(value)
        if value.ndim != ndim or not value.size or not np.isfinite(value).all():
            raise ValueError('Smoothing requires finite nonempty actions')
        if self.joint_names is not None and value.shape[-1] != len(self.joint_names):
            raise ValueError('Smoothing action joint count differs from joint names')
        return value

    def _roughness(self, values):
        if len(values) < 3 or not self.arm_indices:
            return 0.
        delta = np.diff(np.asarray(values, dtype=np.float64)[:, self.arm_indices], n=2, axis=0)
        return float(np.sqrt(np.mean(delta**2)))

    def chunk(self, actions, protected_prefix=0):
        actions = self._validate(actions, 2)
        if not isinstance(protected_prefix, (int, np.integer)) or not 0 <= protected_prefix <= len(actions):
            raise ValueError('Invalid committed action prefix length')
        if self.method == 'none':
            return actions.copy()
        result = smooth_action_chunk(actions, self.arm_indices, protected_prefix)
        self.raw_roughness_rad = self._roughness(actions[protected_prefix:])
        self.filtered_roughness_rad = self._roughness(result[protected_prefix:])
        return result

    def action(self, action):
        """Causal three-point average after per-step corrections (one-step lag)."""
        action = self._validate(action, 1)
        if self.method == 'none':
            return action.copy()
        if not self.inputs:
            self.inputs.extend([action.copy(), action.copy()])
        self.inputs.append(action.copy())
        result = np.array(action, dtype=np.result_type(action.dtype, np.float32), copy=True)
        values = list(self.inputs)
        result[self.arm_indices] = (.25*values[0][self.arm_indices] + .5*values[1][self.arm_indices]
                                    + .25*values[2][self.arm_indices])
        self.outputs.append(result.copy())
        self.raw_roughness_rad = self._roughness(self.inputs)
        self.filtered_roughness_rad = self._roughness(self.outputs)
        return result

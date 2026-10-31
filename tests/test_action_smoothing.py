"""Shared processing contracts for standard, prefix, and corrected inference."""
import numpy as np
import pytest

from so101_core.action_smoothing import ActionSmoother
from so101_core.policy import ChunkScheduler


def test_committed_prefix_and_gripper_are_exact_while_future_arm_goals_are_filtered():
    raw = np.array([[0, 0], [4, 0], [0, 1], [0, 1]], dtype=np.float32)
    filtered = ActionSmoother('three_point', ['q', 'gripper']).chunk(raw, 2)
    np.testing.assert_array_equal(filtered[:2], raw[:2])
    np.testing.assert_array_equal(filtered[:, 1], raw[:, 1])
    np.testing.assert_array_equal(filtered[:, 0], [0, 4, 1, 0])


def test_final_command_filter_is_causal_preserves_gripper_and_resets_history():
    smoother = ActionSmoother('three_point', ['q', 'gripper'])
    raw = np.array([[0, 0], [4, 1], [0, 1], [0, 0], [0, 1]], dtype=np.float32)
    filtered = np.stack([smoother.action(action) for action in raw])
    np.testing.assert_array_equal(filtered[:, 0], [0, 1, 2, 1, 0])
    np.testing.assert_array_equal(filtered[:, 1], raw[:, 1])
    smoother.reset()
    np.testing.assert_array_equal(smoother.action(np.array([12, 1])), [12, 1])


def test_standard_scheduler_filters_once_before_consumption_and_reset():
    class Policy:
        def predict(self, observation):
            return np.array([[0, 0], [0, 0], [4, 1], [0, 1], [0, 1]], dtype=np.float32)
    scheduler = ChunkScheduler(3, smoothing='three_point', joint_names=['q', 'gripper'])
    values = np.stack([scheduler.next(Policy(), {}) for _ in range(3)])
    np.testing.assert_array_equal(values, [[0, 0], [1, 0], [2, 1]])
    scheduler.reset()
    np.testing.assert_array_equal(scheduler.next(Policy(), {}), [0, 0])


def test_disabled_processing_is_exact_and_short_predictions_are_supported():
    raw = np.array([[1.25, 0], [3.75, 1]], dtype=np.float32)
    np.testing.assert_array_equal(ActionSmoother('none', ['q', 'gripper']).chunk(raw), raw)
    single = raw[:1]
    np.testing.assert_array_equal(ActionSmoother('three_point', ['q', 'gripper']).chunk(single), single)


@pytest.mark.parametrize('value', [np.nan, np.inf])
def test_invalid_actions_are_not_hidden_by_gripper_exclusion(value):
    smoother = ActionSmoother('three_point', ['q', 'gripper'])
    with pytest.raises(ValueError, match='finite'):
        smoother.chunk(np.array([[0, value]]))
    with pytest.raises(ValueError, match='finite'):
        smoother.action(np.array([0, value]))


def test_names_shape_and_prefix_are_validated():
    with pytest.raises(ValueError, match='joint names'):
        ActionSmoother('three_point')
    smoother = ActionSmoother('three_point', ['q'])
    with pytest.raises(ValueError, match='joint count'):
        smoother.chunk(np.zeros((3, 2)))
    with pytest.raises(ValueError, match='prefix'):
        smoother.chunk(np.zeros((3, 1)), 4)

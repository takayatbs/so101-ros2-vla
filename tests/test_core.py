from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from so101_core.config import load_system, config_hash
from so101_core.control import Mode, ModeMachine, InvalidAction, SafetyFilter, reorder, map_leader
from so101_core.preprocessing import ImagePreprocessor, image_from_ros
from so101_core.synchronization import Sample, Timeline, synchronized_frames
from so101_core.policy import ChunkScheduler
from so101_core.hardware import SO101Bus

CONFIG = Path(__file__).resolve().parents[1]/"config/runtime/system.yaml"


@pytest.mark.parametrize("values", [[float('nan')], [float('inf')], [0, 1]])
def test_reject_bad_actions(values):
    with pytest.raises(InvalidAction):
        reorder(["a"], values, ["a"])


def test_duplicate_or_missing_joints_rejected():
    for names in (["a", "a"], ["a", "c"]):
        with pytest.raises(InvalidAction):
            reorder(names, [0, 1], ["a", "b"])
    np.testing.assert_equal(reorder(["b", "a"], [1, 2], ["a", "b"]), [2, 1])


@pytest.mark.parametrize("stop", [Mode.ESTOP, Mode.FAULT])
def test_latched_stop_needs_reset_and_returns_idle(stop):
    machine = ModeMachine()
    machine.set("TELEOP")
    machine.stop("test", estop=stop==Mode.ESTOP)
    with pytest.raises(ValueError):
        machine.set("IDLE")
    machine.reset()
    assert machine.mode == Mode.IDLE


def test_mode_switch_requires_idle():
    m = ModeMachine()
    m.set("TELEOP")
    with pytest.raises(ValueError):
        m.set("AUTONOMOUS")


def test_si_limits_velocity_acceleration_and_braking():
    limits = {"a": {"min": -1., "max": 1., "velocity": .8, "acceleration": 2.}}
    f = SafetyFilter(["a"], limits)
    f.reset([0.])
    prev_v = 0.0
    for target in [100.]*300+[-100.]*600+[.1]*400:
        previous = f.position.copy()
        q = f.step([target], .01)
        v = float((q-previous)[0]/.01)
        assert -1.-1e-8 <= q[0] <= 1.+1e-8
        assert abs(v) <= .8+1e-8
        assert abs(v-prev_v) <= 2*.01+1e-8
        prev_v = v
    assert abs(f.position[0]-.1) < .01


def test_jump_reject_and_invalid_start():
    f = SafetyFilter(["a"], {"a": {"min": -1, "max": 1, "velocity": 1, "acceleration": 2}}, .2, "reject")
    with pytest.raises(InvalidAction): f.reset([2])
    f.reset([0])
    with pytest.raises(InvalidAction): f.step([.5], .01)


def test_mapping_is_configuration_driven():
    c = load_system(CONFIG)
    names = list(c["teleop"]["joint_mapping"])
    q = map_leader(names, [0]*5+[1], c["robot"]["joints"], c["teleop"]["joint_mapping"], c["robot"]["limits"])
    assert q[-1] == pytest.approx(c["teleop"]["joint_mapping"]["leader_gripper"]["scale"])


def test_native_si_round_trip_including_gripper():
    bus = SO101Bus.__new__(SO101Bus)
    bus.joints = ["shoulder_pan", "gripper"]
    bus.gripper = {"closed_rad": 0., "open_rad": 2.2}
    bus.calibration = {"shoulder_pan": {"range_min": 500, "range_max": 3500},
                       "gripper": {"range_min": 1000, "range_max": 2434}}
    native = {"shoulder_pan": 2500, "gripper": 1500}
    np.testing.assert_allclose(bus.to_si(native), [500*2*np.pi/4095, 500*2*np.pi/4095])
    converted = bus.from_si(bus.to_si(native))
    assert converted == pytest.approx(native)


def test_image_stride_rgb_and_shared_geometry():
    # Two pixels and two padding bytes per row, BGR -> RGB.
    msg = SimpleNamespace(height=1, width=2, step=8, encoding="bgr8", data=bytes([1,2,3,4,5,6,0,0]))
    rgb = image_from_ros(msg)
    np.testing.assert_equal(rgb, [[[3,2,1],[6,5,4]]])
    c = load_system(CONFIG)["observation"]
    p = ImagePreprocessor(c)
    converted = p("top", rgb)
    assert converted.shape == p.shape("top")
    assert converted.dtype == np.uint8
    assert np.array_equal(converted, p("top", rgb))


def test_timestamp_alignment_and_no_future_action():
    state = Timeline([Sample(0., [0.]), Sample(.02, [2.])])
    np.testing.assert_allclose(state.interpolate(.01, .02), [1.])
    action = Timeline([Sample(0., [10.]), Sample(.02, [20.])])
    assert action.previous(.019, .02).value == [10.]
    with pytest.raises(ValueError): state.interpolate(.03, .02)
    with pytest.raises(ValueError): action.previous(-.01, .02)


def test_camera_skew_and_gaps_are_not_silently_removed():
    states = Timeline([Sample(i*.01, [i]) for i in range(20)])
    actions = states
    cameras = {"a": Timeline([Sample(0, "a"), Sample(.1, "a")]),
               "b": Timeline([Sample(.03, "b"), Sample(.13, "b")])}
    with pytest.raises(ValueError):
        list(synchronized_frames(states, actions, cameras, 30, .02, .05))


def test_chunk_consumption_and_reset():
    class FakePolicy:
        count = 0
        def predict(self, _):
            self.count += 1
            return np.array([[1,2],[3,4],[5,6]])
    policy, scheduler = FakePolicy(), ChunkScheduler(2)
    np.testing.assert_equal(scheduler.next(policy, {}), [1,2])
    np.testing.assert_equal(scheduler.next(policy, {}), [3,4])
    np.testing.assert_equal(scheduler.next(policy, {}), [1,2])
    assert policy.count == 2
    scheduler.reset()
    scheduler.next(policy, {})
    assert policy.count == 3
    with pytest.raises(ValueError): ChunkScheduler(2, "rtc")


def test_config_hash_is_path_independent_and_schema_sensitive():
    c = load_system(CONFIG)["observation"]
    before = config_hash(c)
    c["_root"] = "/elsewhere"
    assert config_hash(c) == before
    c["cameras"] = ["side"]
    assert config_hash(c) != before


def test_mujoco_adapter_when_available():
    pytest.importorskip("mujoco")
    from so101_core.hardware import MujocoFollower
    c = load_system(CONFIG)
    model = Path(__file__).resolve().parents[1]/"ros2_ws/src/so101_description/mujoco/so101.xml"
    sim = MujocoFollower(c["robot"]["joints"], model, 100)
    sim.enable(True)
    sim.write([.1]*6)
    for _ in range(100): q = sim.read()
    assert np.isfinite(q).all()
    assert np.linalg.norm(q) > .01

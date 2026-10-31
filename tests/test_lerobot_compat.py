"""Regression vectors generated from actual LeRobot methods at the pinned commit.

The fixture contains both degree and normalized modes, encoder endpoints,
half-tick midpoints and commands beyond measured calibration ranges.
"""
import copy
import json
from pathlib import Path
import numpy as np
import pytest

from so101_core.config import load_system
from so101_core.control import follower_goal, make_control_filter, map_leader, InvalidAction
from so101_core.feetech import TICK_RAD, encode_value, decode_value, ServoStatusError
from so101_core.hardware import SO101Bus
from so101_core.lerobot_compat import LeRobotUnits, REFERENCE_COMMIT

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = json.loads((ROOT/"tests/fixtures/lerobot_so101_reference.json").read_text())


@pytest.mark.parametrize("case", REFERENCE["cases"], ids=lambda c: f"{c['role']}-degrees-{c['use_degrees']}")
def test_native_units_match_pinned_lerobot_exactly(case):
    assert REFERENCE["reference_commit"] == REFERENCE_COMMIT
    raw = case["calibration"]
    grip = raw["gripper"]
    gripper = {"closed_rad": 0., "open_rad": (grip["range_max"]-grip["range_min"])*TICK_RAD}
    units = LeRobotUnits(list(raw), raw, gripper, case["use_degrees"])
    for sample in case["read"]:
        assert units.normalize(sample["raw"]) == sample["native"]
        si = units.to_si(sample["native"])
        np.testing.assert_allclose(list(units.to_native(si).values()), list(sample["native"].values()), atol=1e-12)
    for sample in case["write"]:
        assert units.unnormalize(sample["native"]) == sample["raw"]


def test_default_commands_are_not_clipped_or_slowed_by_old_limits():
    config = load_system(ROOT/"config/runtime/system.yaml")
    robot = config["robot"]
    assert robot["control_profile"] == "lerobot"
    assert robot["follower"]["max_relative_target"] is None
    f = make_control_filter(robot, config["safety"])
    # A legitimate resting pose formerly rejected by the +/-1.5 rad bounds.
    present = np.array([0., -1.9456, 1.7092, 1.3, 0., 0.])
    f.reset(present)
    target = np.array([.8, 2.1, -2., -.5, 3., 1.])
    np.testing.assert_array_equal(f.step(target, 1/60), target)
    np.testing.assert_array_equal(follower_goal(robot, target, present), target)
    mapped = map_leader(list(config["teleop"]["joint_mapping"]), target, robot["joints"],
                        config["teleop"]["joint_mapping"])
    np.testing.assert_array_equal(mapped[:5], target[:5])
    with pytest.raises(InvalidAction):
        f.step([float("nan")]*6, 1/60)


def test_relative_cap_is_native_degrees_and_gripper_percent_from_measured_state():
    config = load_system(ROOT/"config/runtime/system.yaml")
    robot = copy.deepcopy(config["robot"])
    robot["follower"]["max_relative_target"] = 5.
    spec = robot["follower"]
    raw = next(c['calibration'] for c in REFERENCE['cases'] if c['role'] == 'follower')
    units = LeRobotUnits(robot["joints"], raw, spec["gripper"])
    present = units.to_si(dict.fromkeys(robot["joints"], 20.))
    target = units.to_si(dict.fromkeys(robot["joints"], 80.))
    limited = follower_goal(robot, target, present, units)
    np.testing.assert_allclose(list(units.to_native(limited).values()), [25.]*6)
    limits = dict.fromkeys(robot["joints"], 2.)
    limits["gripper"] = 10.
    limited = units.relative_goal(target, present, limits)
    np.testing.assert_allclose(list(units.to_native(limited).values()), [22.]*5+[30.])
    with pytest.raises(ValueError):
        units.relative_goal(target, present, {"gripper": 2.})


@pytest.mark.parametrize("role", ["leader", "follower"])
@pytest.mark.parametrize("profile", ["lerobot", "bounded"])
def test_servo_initialization_matches_role_and_never_enables_or_moves(tmp_path, monkeypatch, role, profile):
    from so101_core import hardware
    config = load_system(ROOT/"config/runtime/system.yaml")
    robot = config["robot"]
    raw = next(c['calibration'] for c in REFERENCE['cases'] if c['role'] == role)
    span = (raw['gripper']['range_max']-raw['gripper']['range_min'])*TICK_RAD
    robot[role]['gripper'] = {'closed_rad': 0., 'open_rad': span}
    path = tmp_path/"arm.json"
    path.write_text(json.dumps(raw))

    class FakeBus:
        def __init__(self, *args, **kwargs):
            self.is_connected, self.writes, self.handshake = False, [], False
            self.allowed_status = {}
        def connect(self): self.is_connected = True
        def close(self): self.is_connected = False
        def check_motors(self): self.handshake = True
        def read_calibration(self):
            assert not self.allowed_status
            return raw
        def disable_torque(self):
            for joint in raw: self.write("Torque_Enable", joint, 0)
        def read(self, register, joint):
            assert register == "Phase"
            assert not self.allowed_status
            return 0x5c
        def write(self, register, joint, value): self.writes.append((register, joint, value))

    fake = FakeBus()
    monkeypatch.setattr(hardware, "ServoBus", lambda *args, **kw: fake)
    adapter = SO101Bus("unused", path, robot["joints"], robot[role]["gripper"],
                       motors=robot[role]["motors"], role=role, profile=profile,
                       servo_settings=robot["servo_settings"])
    assert fake.allowed_status == ({6: 0x20} if role == "follower" and profile == "lerobot" else {})
    assert fake.handshake
    assert not any(reg == "Goal_Position" or (reg == "Torque_Enable" and value) for reg, _, value in fake.writes)
    for joint in raw:
        for reg, val in [("Return_Delay_Time", 0), ("Maximum_Acceleration", 254),
                         ("Acceleration", 254), ("Phase", 0x4c), ("Operating_Mode", 0)]:
            assert (reg, joint, val) in fake.writes
    if role == "follower":
        for joint in raw:
            for reg, val in [("P_Coefficient", 16), ("I_Coefficient", 0), ("D_Coefficient", 32)]:
                assert (reg, joint, val) in fake.writes
        for reg, val in [("Max_Torque_Limit", 500), ("Protection_Current", 250), ("Overload_Torque", 25)]:
            assert (reg, "gripper", val) in fake.writes
    else:
        assert not any(reg in ("P_Coefficient", "I_Coefficient", "D_Coefficient", "Max_Torque_Limit", "Protection_Current", "Overload_Torque")
                       for reg, _, _ in fake.writes)
    adapter.close()
    assert not fake.is_connected


def test_lerobot_goal_sign_magnitude_and_position_retries():
    assert encode_value("Goal_Position", -100) == 0x8064
    assert decode_value("Goal_Position", 0x8064) == -100
    driver = SO101Bus.__new__(SO101Bus)
    driver.num_read_retries = 2
    driver.to_si = lambda raw: np.array(list(raw.values()))
    class TransientBus:
        calls = 0
        def read_all(self, register):
            self.calls += 1
            if self.calls < 3:
                raise IOError("temporary bad packet")
            return {"joint": 2047}
    driver.bus = TransientBus()
    np.testing.assert_equal(driver.read(), [2047])
    assert driver.bus.calls == 3
    driver.bus.calls = 0
    driver.num_read_retries = 1
    with pytest.raises(IOError):
        driver.read()
    class AlarmBus:
        calls = 0
        def read_all(self, register):
            self.calls += 1
            raise ServoStatusError(6, 1)
    driver.bus = AlarmBus()
    with pytest.raises(ServoStatusError, match="input voltage alarm"):
        driver.read()
    assert driver.bus.calls == 1


def test_apply_preserves_original_lerobot_calibration_and_derives_ranges(tmp_path, monkeypatch):
    import shutil
    from so101_core.calibration import apply, check
    monkeypatch.setenv('SO101_USE_LOCAL_CONFIG', '1')
    shutil.copytree(ROOT/"config", tmp_path/"config",
                    ignore=shutil.ignore_patterns('*.local.yaml', 'leader.json', 'follower.json'))
    for role in ('leader', 'follower'):
        raw = next(c['calibration'] for c in REFERENCE['cases'] if c['role'] == role)
        (tmp_path/f'config/calibration/{role}.json').write_text(json.dumps(raw))
    before = {p:p.read_bytes() for p in (tmp_path/"config/calibration").glob("*.json")}
    config = load_system(tmp_path/"config/runtime/system.yaml")
    apply(config)
    config = load_system(tmp_path/"config/runtime/system.yaml")
    check(config)
    for path, content in before.items():
        assert path.read_bytes() == content
    raw = json.loads((tmp_path/"config/calibration/follower.json").read_text())
    for joint in config["robot"]["joints"][:-1]:
        half = (raw[joint]["range_max"]-raw[joint]["range_min"])*TICK_RAD/2
        assert config["robot"]["limits"][joint]["min"] == pytest.approx(-half)
        assert config["robot"]["limits"][joint]["max"] == pytest.approx(half)

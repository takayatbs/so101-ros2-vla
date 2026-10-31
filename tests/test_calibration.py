import shutil
import copy
import json
import math
from pathlib import Path
import pytest
import yaml
from so101_core.config import load_system, resolve
from so101_core.calibration import apply, check, measure, measure_on_bus, settings_from_calibration
from so101_core.hardware import validate_calibration
from so101_core.feetech import TICK_RAD

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    monkeypatch.setenv('SO101_USE_LOCAL_CONFIG', '1')
    folder = tmp_path/"config"
    folder.mkdir()
    shutil.copytree((SOURCE/"config"), folder, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('*.local.yaml', 'leader.json', 'follower.json'))
    (folder/"calibration").mkdir(exist_ok=True)
    # No machine-specific files required in CI.
    config = load_system(folder/"runtime/system.real30hz.yaml")
    for role in ("leader", "follower"):
        raw = {j: {"id": i, "drive_mode": 0, "homing_offset": 0, "range_min": 500,
                   "range_max": 3500 if j != "gripper" else (1500 if role == "leader" else 2000)}
               for j, i in config["robot"][role]["motors"].items()}
        resolve(config["robot"], config["robot"][role]["calibration"]).write_text(json.dumps(raw))
    return config


class SimBus:
    """Fake register state: offset changes affect measured positions."""
    def __init__(self, motors):
        self.motors = motors
        self.port = "simulated"
        self.data = {j: {"Torque_Enable": 0, "Lock": 1, "Operating_Mode": 0, "Phase": 16,
                         "Homing_Offset": 100, "Min_Position_Limit": 500, "Max_Position_Limit": 3500}
                     for j in motors}
        self.position = {j: 2047 for j in motors}
        self.writes, self.is_connected = [], False

    def connect(self): self.is_connected = True
    def close(self): self.is_connected = False

    def read(self, register, name):
        if register == "Present_Position":
            return self.position[name]-self.data[name]["Homing_Offset"]
        return self.data[name][register]

    def write(self, register, name, value):
        self.writes.append((register, name, value))
        self.data[name][register] = value

    def read_all(self, register): return {j: self.read(register, j) for j in self.motors}
    def disable_torque(self):
        for j in self.motors: self.write("Torque_Enable", j, 0)

    def read_calibration(self):
        return {j: {"id": i, "drive_mode": 0, "homing_offset": self.data[j]["Homing_Offset"],
                    "range_min": self.data[j]["Min_Position_Limit"], "range_max": self.data[j]["Max_Position_Limit"]}
                for j, i in self.motors.items()}

    def write_calibration(self, raw):
        for j, item in raw.items():
            for key, reg in [("homing_offset", "Homing_Offset"), ("range_min", "Min_Position_Limit"), ("range_max", "Max_Position_Limit")]:
                self.write(reg, j, item[key])
        assert self.read_calibration() == raw


def test_measurement_all_joints_without_torque_or_goals(configuration):
    bus = SimBus(configuration["robot"]["leader"]["motors"])
    def respond(prompt):
        if "閉じた" in prompt: bus.position["gripper"] = 1400
        if "開いた" in prompt: bus.position["gripper"] = 3000
    result = measure_on_bus(bus, respond, lambda b, j: (600, 3400))
    assert result["gripper"]["range_min"] == 1400 and result["gripper"]["range_max"] == 3000
    assert result["wrist_roll"]["range_min"] == 0 and result["wrist_roll"]["range_max"] == 4095
    assert all(v["homing_offset"] == 0 for v in result.values())
    assert not any(r == "Goal_Position" or (r == "Torque_Enable" and v != 0) for r, _, v in bus.writes)
    assert bus.read_calibration() == result


def test_aborted_measurement_restores_servo_and_preserves_json(configuration, monkeypatch):
    from so101_core import calibration as module
    bus = SimBus(configuration["robot"]["leader"]["motors"])
    original = copy.deepcopy(bus.data)
    path = resolve(configuration["robot"], configuration["robot"]["leader"]["calibration"])
    before = path.read_bytes()
    monkeypatch.setattr(module, "make_bus", lambda c, r: bus)
    monkeypatch.setattr(module.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    def abort(bus):
        bus.write("Homing_Offset", "shoulder_pan", 1234)
        raise KeyboardInterrupt()
    monkeypatch.setattr(module, "measure_on_bus", abort)
    with pytest.raises(KeyboardInterrupt): measure(configuration, "leader")
    assert path.read_bytes() == before
    assert bus.data["shoulder_pan"]["Homing_Offset"] == original["shoulder_pan"]["Homing_Offset"]
    assert not bus.is_connected
    assert yaml.safe_load(Path(configuration["robot"]["_file"]).read_text())["hardware_qualified"] is False
    assert list(Path(configuration["robot"]["_config_root"]).parent.glob("data/calibration/*/leader-servo-before.json"))


def test_apply_closed_offsets_scale_backup_and_check(configuration):
    public_robot = Path(configuration['robot']['_file'])
    public_teleop = Path(configuration['teleop']['_file'])
    originals = {p: p.read_bytes() for p in (public_robot, public_teleop)}
    configuration["robot"]["leader"]["gripper"]["closed_rad"] = .1
    configuration["robot"]["follower"]["gripper"]["closed_rad"] = .2
    apply(configuration)
    assert all(path.read_bytes() == original for path, original in originals.items())
    assert configuration['robot']['_file'].endswith('robot.local.yaml')
    assert configuration['teleop']['_file'].endswith('teleop.local.yaml')
    loaded = load_system(Path(configuration["robot"]["_config_root"])/"runtime/system.real30hz.yaml")
    assert loaded["robot"]["follower"]["gripper"]["open_rad"] == pytest.approx(.2+1500*TICK_RAD)
    mapping = loaded["teleop"]["joint_mapping"]["leader_gripper"]
    assert mapping["scale"] == pytest.approx(1.5) and mapping["offset"] == pytest.approx(.05)
    check(loaded)
    assert list(Path(configuration["robot"]["_config_root"]).parent.glob("data/calibration/*/*robot.yaml"))


def test_bad_calibration_does_not_modify_yaml(configuration):
    path = resolve(configuration["robot"], configuration["robot"]["leader"]["calibration"])
    raw = json.loads(path.read_text()); raw["gripper"]["range_max"] = raw["gripper"]["range_min"]
    path.write_text(json.dumps(raw))
    target = Path(configuration["robot"]["_file"]); before = target.read_bytes()
    with pytest.raises(ValueError): apply(configuration)
    assert target.read_bytes() == before


def test_restore_recovers_servo_and_json_without_enabling(configuration, monkeypatch):
    from so101_core import calibration as module
    bus = SimBus(configuration['robot']['leader']['motors'])
    snapshot = {'calibration':bus.read_calibration(), 'phase':bus.read_all('Phase'), 'mode':bus.read_all('Operating_Mode')}
    path = Path(configuration['robot']['_root'])/'snapshot.json'
    path.write_text(json.dumps(snapshot))
    bus.write('Homing_Offset','shoulder_pan',999)
    monkeypatch.setattr(module,'make_bus',lambda c,r:bus)
    monkeypatch.setattr('builtins.input',lambda _: 'yes')
    module.restore(configuration,'leader',path)
    assert bus.read_calibration() == snapshot['calibration']
    restored=json.loads(resolve(configuration['robot'],configuration['robot']['leader']['calibration']).read_text())
    assert restored==snapshot['calibration']
    assert not bus.is_connected
    assert not any(r=='Goal_Position' or (r=='Torque_Enable' and v!=0) for r,_,v in bus.writes)


def test_single_motor_setup_verifies_new_id_and_baud(configuration, monkeypatch):
    from so101_core import calibration as module
    class SetupBus(SimBus):
        def __init__(self):
            super().__init__({'shoulder_pan':9})
            self.data['shoulder_pan'].update(Model_Number=777,ID=9,Baud_Rate=4)
            self.baudrate=115200
            self.changed_ack=[]
        def write(self,register,name,value,expect_reply=True):
            if register in ('ID','Baud_Rate'): self.changed_ack.append(expect_reply)
            super().write(register,name,value)
    bus=SetupBus()
    monkeypatch.setattr(module,'ServoBus',lambda *a,**kw:bus)
    monkeypatch.setattr('builtins.input',lambda _: 'ONE')
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    module.setup_motor(configuration,'leader','shoulder_pan',9,115200)
    assert bus.data['shoulder_pan']['ID']==1
    assert bus.data['shoulder_pan']['Baud_Rate']==0
    assert bus.motors['shoulder_pan']==1 and bus.baudrate==1000000
    assert bus.changed_ack==[False,False]
    assert not bus.is_connected


def test_inspect_reads_actual_servo_settings_and_never_writes(configuration, monkeypatch):
    from so101_core import calibration as module
    class InspectBus(SimBus):
        def __init__(self):
            super().__init__(configuration['robot']['leader']['motors'])
            self.baudrate=1000000
            for name,i in self.motors.items():
                self.data[name].update(ID=i,Baud_Rate=0,Model_Number=777,Torque_Enable=1)
    bus=InspectBus()
    monkeypatch.setattr(module,'make_bus',lambda c,r:bus)
    result=module.inspect_arm(configuration,'leader')
    assert len(result['motors'])==6 and result['read_only']
    assert result['motors']['gripper']['id']==6
    assert all(v['torque_enable']==1 for v in result['motors'].values())
    assert not bus.writes and not bus.is_connected
    bus.data['gripper']['ID']=9
    with pytest.raises(ValueError,match='ID/baud settings mismatch'):
        module.inspect_arm(configuration,'leader')
    assert not bus.writes and not bus.is_connected

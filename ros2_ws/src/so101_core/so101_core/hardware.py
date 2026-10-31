import json
import math
import numpy as np
from .feetech import ServoBus, TICK_RAD, ServoStatusError
from .lerobot_compat import LeRobotUnits


def validate_calibration(raw, joints, motors=None):
    if set(raw) != set(joints):
        raise ValueError("Calibration must contain all six configured joints")
    ids = []
    for name in joints:
        item = raw[name]
        if set(item) != {"id", "drive_mode", "homing_offset", "range_min", "range_max"}:
            raise ValueError(f"Invalid calibration schema: {name}")
        if any(type(v) is not int for v in item.values()):
            raise ValueError(f"Calibration values must be integers: {name}")
        if not 1 <= item["id"] <= 253 or item["drive_mode"] != 0:
            raise ValueError(f"Unsupported motor ID/drive mode: {name}")
        if abs(item["homing_offset"]) > 2047 or not 0 <= item["range_min"] < item["range_max"] <= 4095:
            raise ValueError(f"Invalid calibration offset/range: {name}")
        if motors is not None and item["id"] != motors[name]:
            raise ValueError(f"Calibration/config motor ID mismatch: {name}")
        ids.append(item["id"])
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate motor IDs")
    return raw


class SO101Bus:
    """Project-owned serial/STS3215 driver; ROS units are radians throughout."""
    def __init__(self, port, calibration, joints, gripper, motors=None,
                 baudrate=1000000, timeout_s=.05, read_only=False, role="follower",
                 profile="bounded", use_degrees=True, num_read_retries=2,
                 servo_settings=None, disable_torque_on_disconnect=True):
        raw = validate_calibration(json.loads(open(calibration).read()), joints, motors)
        span = (raw["gripper"]["range_max"]-raw["gripper"]["range_min"])*TICK_RAD
        if not math.isclose(gripper["open_rad"]-gripper["closed_rad"], span, rel_tol=1e-6):
            raise ValueError("Gripper SI span must match the calibrated servo range; run calibrate.sh apply")
        self.joints, self.gripper, self.calibration = list(joints), gripper, raw
        self.role, self.profile = role, profile
        if role not in ("leader", "follower") or profile not in ("lerobot", "bounded"):
            raise ValueError("Invalid SO101 role/profile")
        if type(num_read_retries) is not int or num_read_retries < 0:
            raise ValueError("num_read_retries must be a nonnegative integer")
        self.units = LeRobotUnits(self.joints, raw, gripper, use_degrees)
        self.num_read_retries = num_read_retries
        self.disable_torque_on_disconnect = disable_torque_on_disconnect
        self.read_only = read_only
        self.bus = ServoBus(port, {name: raw[name]["id"] for name in joints}, baudrate, timeout_s)
        try:
            self.bus.connect()
            if read_only:
                return
            self.bus.check_motors()
            self.bus.disable_torque()
            if not self.calibration_matches():
                raise ValueError("Servo/file calibration mismatch; run scripts/calibrate.sh before real runtime")
            self.configure(servo_settings or {})
            if self.role == "follower" and self.profile == "lerobot":
                # LeRobot sync-read permits grasping while the gripper reports
                # overload. Preserve valid feedback and the servo's own protection.
                # Only the gripper's overload bit is advisory during operation.
                self.bus.allowed_status = {raw["gripper"]["id"]: 0x20}
        except Exception:
            if self.bus.is_connected:
                try:
                    if not read_only:
                        self.bus.disable_torque()
                finally:
                    self.bus.close()
            raise

    def configure(self, settings):
        """LeRobot configure_motors + role-specific configure, torque already OFF."""
        if self.read_only:
            raise RuntimeError("Read-only connection")
        for name in self.joints:
            self.bus.write("Lock", name, 0)
            self.bus.write("Return_Delay_Time", name, settings.get("return_delay_time", 0))
            self.bus.write("Maximum_Acceleration", name, settings.get("maximum_acceleration", 254))
            self.bus.write("Acceleration", name, settings.get("acceleration", 254))
            phase = self.bus.read("Phase", name)
            if phase & 0x10:
                self.bus.write("Phase", name, phase & ~0x10)
            self.bus.write("Operating_Mode", name, 0)
            if self.role == "follower":
                for key, register, default in [("position_p_coefficient", "P_Coefficient", 16),
                                               ("position_i_coefficient", "I_Coefficient", 0),
                                               ("position_d_coefficient", "D_Coefficient", 32)]:
                    self.bus.write(register, name, settings.get(key, default))
        if self.role == "follower":
            for key, register, default in [("gripper_max_torque_limit", "Max_Torque_Limit", 500),
                                          ("gripper_protection_current", "Protection_Current", 250),
                                          ("gripper_overload_torque", "Overload_Torque", 25)]:
                self.bus.write(register, "gripper", settings.get(key, default))

    def calibration_matches(self):
        return self.bus.read_calibration() == self.calibration

    def to_si(self, values):
        if getattr(self, "profile", "bounded") == "lerobot":
            return self.units.to_si(self.units.normalize(values))
        result = []
        for name in self.joints:
            item = self.calibration[name]
            if name == "gripper":
                tick = min(item["range_max"], max(item["range_min"], values[name]))
                angle = self.gripper["closed_rad"] + (tick-item["range_min"])*TICK_RAD
            else:
                angle = (values[name]-(item["range_min"]+item["range_max"])/2)*TICK_RAD
            result.append(angle)
        return np.asarray(result)

    def from_si(self, values):
        values = np.asarray(values)
        if values.shape != (len(self.joints),) or not np.isfinite(values).all():
            raise ValueError("Expected finite joint positions")
        if getattr(self, "profile", "bounded") == "lerobot":
            return self.units.unnormalize(self.units.to_native(values))
        result = {}
        for name, angle in zip(self.joints, values, strict=True):
            item = self.calibration[name]
            if name == "gripper":
                angle = np.clip(angle, self.gripper["closed_rad"], self.gripper["open_rad"])
                tick = (angle-self.gripper["closed_rad"])/TICK_RAD+item["range_min"]
            else:
                tick = angle/TICK_RAD+(item["range_min"]+item["range_max"])/2
            tick = int(tick)
            if not 0 <= tick <= 4095:
                raise ValueError(f"Goal outside encoder range: {name}")
            result[name] = tick
        return result

    def read(self):
        for attempt in range(self.num_read_retries+1):
            try:
                raw = self.bus.read_all("Present_Position")
                break
            except ServoStatusError:
                raise
            except (OSError, TimeoutError):
                if attempt == self.num_read_retries:
                    raise
        if any(not 0 <= value <= 4095 for value in raw.values()):
            raise ValueError("Position feedback is outside single-turn range; check servo Phase/calibration")
        return self.to_si(raw)

    def write(self, values):
        if self.read_only:
            raise RuntimeError("Read-only connection")
        self.bus.write_all("Goal_Position", self.from_si(values))

    def enable(self, enabled):
        if self.read_only:
            raise RuntimeError("Read-only connection")
        if enabled:
            for name in self.joints:
                self.bus.write("Torque_Enable", name, 1)
        else:
            self.bus.disable_torque()

    def diagnostics(self):
        return {name: self.bus.read_all(register)
                for name, register in [("temperature", "Present_Temperature"),
                                       ("voltage", "Present_Voltage"), ("load", "Present_Load"),
                                       ("current", "Present_Current")]}

    def consume_status(self):
        flags = self.bus.consume_status()
        return {name: flags[item["id"]] for name, item in self.calibration.items()
                if flags.get(item["id"], 0)}

    def close(self):
        try:
            if self.bus.is_connected and not self.read_only and self.disable_torque_on_disconnect:
                self.bus.disable_torque()
        finally:
            self.bus.close()


class MockFollower:
    def __init__(self, joints):
        self.q = np.zeros(len(joints))
        self.goal = self.q.copy()
        self.enabled = False

    def read(self):
        if self.enabled:
            self.q += np.clip(self.goal-self.q, -0.01, 0.01)
        return self.q.copy()

    def write(self, values):
        self.goal = np.array(values, copy=True)

    def enable(self, enabled):
        self.enabled = enabled

    def close(self):
        self.enabled = False


class MujocoFollower(MockFollower):
    def __init__(self, joints, model_path, rate):
        import mujoco
        super().__init__(joints)
        self.mj = mujoco
        self.model = mujoco.MjModel.from_xml_path(str(model_path))
        self.model.opt.timestep = 1/rate
        self.data = mujoco.MjData(self.model)
        self.indices = [mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, j) for j in joints]
        if any(index < 0 for index in self.indices):
            raise ValueError("MuJoCo joint schema mismatch")
        self.addresses = [int(self.model.jnt_qposadr[index]) for index in self.indices]
        mujoco.mj_forward(self.model, self.data)

    def read(self):
        if self.enabled:
            self.data.ctrl[:] = self.goal
            self.mj.mj_step(self.model, self.data)
        else:
            # Initial test model freezes while torque-disabled (no gravity drop).
            self.data.qvel[:] = 0
        return self.data.qpos[self.addresses].copy()

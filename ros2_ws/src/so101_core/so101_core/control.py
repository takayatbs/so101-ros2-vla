from enum import Enum
import numpy as np
from .lerobot_compat import control_profile


class InvalidAction(ValueError):
    pass


def reorder(names, values, expected):
    if len(names) != len(values) or len(set(names)) != len(names) or set(names) != set(expected):
        raise InvalidAction("Joint names/dimensions do not match the schema")
    indexed = dict(zip(names, values, strict=True))
    result = np.asarray([indexed[name] for name in expected], dtype=np.float64)
    if not np.isfinite(result).all():
        raise InvalidAction("NaN/Inf in joint data")
    return result


class Mode(str, Enum):
    IDLE = "IDLE"
    TELEOP = "TELEOP"
    AUTONOMOUS = "AUTONOMOUS"
    MOVEIT = "MOVEIT"
    ESTOP = "ESTOP"
    FAULT = "FAULT"


class ModeMachine:
    def __init__(self):
        self.mode = Mode.IDLE
        self.reason = "startup"

    def set(self, requested):
        target = Mode(requested)
        if self.mode in (Mode.ESTOP, Mode.FAULT):
            raise ValueError("Latched stop: use /reset_fault to return to IDLE")
        if target in (Mode.ESTOP, Mode.FAULT):
            raise ValueError("Use the stop services")
        if self.mode != Mode.IDLE and target not in (Mode.IDLE, self.mode):
            raise ValueError("Return to IDLE before switching command sources")
        self.mode, self.reason = target, "operator"

    def stop(self, reason, estop=False):
        self.mode = Mode.ESTOP if estop or self.mode == Mode.ESTOP else Mode.FAULT
        self.reason = reason

    def reset(self):
        self.mode, self.reason = Mode.IDLE, "operator reset"


class SafetyFilter:
    """Discrete position/velocity/acceleration constraints in SI units.

    Velocity is integrated instead of clipping position after the fact. Braking
    distance bounds prevent arriving at a joint limit with nonzero velocity.
    """
    def __init__(self, joints, limits, jump_threshold=None, jump_behavior="clamp"):
        self.joints = joints
        self.lo = np.array([limits[j]["min"] for j in joints], dtype=float)
        self.hi = np.array([limits[j]["max"] for j in joints], dtype=float)
        self.vmax = np.array([limits[j]["velocity"] for j in joints], dtype=float)
        self.amax = np.array([limits[j]["acceleration"] for j in joints], dtype=float)
        if not (np.isfinite([self.lo, self.hi, self.vmax, self.amax]).all()
                and (self.lo < self.hi).all() and (self.vmax > 0).all() and (self.amax > 0).all()):
            raise ValueError("Invalid safety limits")
        if jump_behavior not in ("clamp", "reject"):
            raise ValueError("Unknown jump behavior")
        self.jump_threshold, self.jump_behavior = jump_threshold, jump_behavior
        self.position = None
        self.velocity = np.zeros(len(joints))

    def reset(self, position):
        position = self.validate(position)
        if (position < self.lo).any() or (position > self.hi).any():
            raise InvalidAction("Measured starting position outside configured limits")
        self.position, self.velocity = position.copy(), np.zeros_like(position)

    def validate(self, values):
        values = np.asarray(values, dtype=float)
        if values.shape != (len(self.joints),) or not np.isfinite(values).all():
            raise InvalidAction("Invalid action dimension or NaN/Inf")
        return values

    def step(self, target, dt):
        target = self.validate(target)
        if self.position is None or not np.isfinite(dt) or dt <= 0:
            raise InvalidAction("Safety filter requires state and a positive time step")
        if self.jump_threshold is not None and self.jump_behavior == "reject":
            if (np.abs(target - self.position) > self.jump_threshold).any():
                raise InvalidAction("Abrupt action change")
        target = np.clip(target, self.lo, self.hi)
        # Conservative stopping speed, including one full control step.
        positive = np.sqrt((self.amax * dt)**2 + 2*self.amax*(self.hi-self.position)) - self.amax*dt
        negative = np.sqrt((self.amax * dt)**2 + 2*self.amax*(self.position-self.lo)) - self.amax*dt
        lower = np.maximum.reduce([-self.vmax, self.velocity-self.amax*dt, -negative])
        upper = np.minimum.reduce([self.vmax, self.velocity+self.amax*dt, positive])
        if (lower > upper + 1e-9).any():
            raise InvalidAction("No feasible safe command; stopping")
        distance = target-self.position
        stopping_speed = np.sqrt((self.amax*dt)**2+2*self.amax*np.abs(distance))-self.amax*dt
        desired = np.sign(distance)*np.minimum(np.abs(distance)/dt, stopping_speed)
        velocity = np.minimum(upper, np.maximum(lower, desired))
        self.position = self.position + velocity*dt
        self.velocity = velocity
        return self.position.copy()


class LeRobotFilter:
    """Validate commands without adding motion limits absent from LeRobot."""
    def __init__(self, joints):
        self.joints, self.position = joints, None

    def reset(self, position):
        self.position = self.validate(position).copy()

    def validate(self, values):
        values = np.asarray(values, dtype=float)
        if values.shape != (len(self.joints),) or not np.isfinite(values).all():
            raise InvalidAction("Invalid action dimension or NaN/Inf")
        return values

    def step(self, target, dt):
        if self.position is None or not np.isfinite(dt) or dt <= 0:
            raise InvalidAction("Control requires state and a positive time step")
        self.position = self.validate(target).copy()
        return self.position.copy()


def make_control_filter(robot, safety):
    if control_profile(robot) == "lerobot":
        return LeRobotFilter(robot["joints"])
    return SafetyFilter(robot["joints"], robot["limits"],
                        safety["jump_threshold_rad"], safety["jump_behavior"])


def follower_goal(robot, command, present, units=None):
    """Last hardware boundary; optional LeRobot cap uses measured position."""
    joints = robot["joints"]
    goal = reorder(joints, command, joints)
    present = reorder(joints, present, joints)
    if control_profile(robot) == "lerobot":
        from .config import resolve
        from .lerobot_compat import LeRobotUnits
        import json
        spec = robot["follower"]
        cap = spec.get("max_relative_target")
        if cap is None:
            return goal
        if units is None:
            units = LeRobotUnits(joints, json.loads(resolve(robot, spec["calibration"]).read_text()),
                                 spec["gripper"], spec.get("use_degrees", True))
        return units.relative_goal(goal, present, cap)
    lo = np.array([robot["limits"][j]["min"] for j in joints])
    hi = np.array([robot["limits"][j]["max"] for j in joints])
    goal = np.clip(goal, lo, hi)
    cap = robot["follower"].get("max_target_step_rad", .05)
    return present+np.clip(goal-present, -cap, cap)


def map_leader(names, positions, joints, mapping, limits=None):
    leader_names = list(mapping)
    q = reorder(names, positions, leader_names)
    targets = {spec["follower"]: q[i]*spec["scale"]+spec["offset"]
               for i, spec in enumerate(mapping.values())}
    if set(targets) != set(joints):
        raise InvalidAction("Mapping must cover every follower joint exactly once")
    if limits is None:
        return np.array([targets[j] for j in joints])
    return np.array([np.clip(targets[j], limits[j]["min"], limits[j]["max"]) for j in joints])

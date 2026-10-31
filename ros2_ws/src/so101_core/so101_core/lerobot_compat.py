# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
# Copyright 2026 The HuggingFace Inc. team. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# LeRobot reference: https://github.com/huggingface/lerobot
# Revision: 22bd7a2f489b367d8df42de803b1e8c4ca63a3f9
# Upstream: src/lerobot/motors/motors_bus.py
# Upstream: src/lerobot/motors/feetech/feetech.py
# Upstream: src/lerobot/robots/so_follower/so_follower.py
# Upstream: src/lerobot/teleoperators/so_leader/so_leader.py
# Project modifications: Independent compatibility functions at ROS radian boundaries and synthetic reference checks.
# License and upstream third-party notices: LICENSES/LeRobot-LICENSE.txt

"""SO-101 behavior matching LeRobot 22bd7a2, with radians at ROS boundaries.

Reference: huggingface/lerobot, motors/motors_bus.py, robots/so_follower,
teleoperators/so_leader and motors/feetech at the commit below. Calibration
files are the original LeRobot files; no new origin or range is inferred.
"""
import math
import numpy as np

REFERENCE_COMMIT = "22bd7a2f489b367d8df42de803b1e8c4ca63a3f9"


def control_profile(robot):
    profile = robot.get("control_profile", "bounded")
    if profile not in ("lerobot", "bounded"):
        raise ValueError("control_profile must be lerobot or bounded")
    return profile


def bus_options(robot, role):
    spec = robot[role]
    return dict(role=role, profile=control_profile(robot),
                use_degrees=spec.get("use_degrees", True),
                num_read_retries=spec.get("num_read_retries", 2),
                servo_settings=robot.get("servo_settings", {}),
                disable_torque_on_disconnect=spec.get("disable_torque_on_disconnect", True))


class LeRobotUnits:
    """Convert physical radians to LeRobot degrees / percentages and back."""
    def __init__(self, joints, calibration, gripper, use_degrees=True):
        self.joints, self.calibration, self.gripper = joints, calibration, gripper
        self.use_degrees = use_degrees

    def to_native(self, values):
        result = {}
        for name, angle in zip(self.joints, values, strict=True):
            if name == "gripper":
                closed, opened = self.gripper["closed_rad"], self.gripper["open_rad"]
                result[name] = (float(angle)-closed)/(opened-closed)*100
            elif self.use_degrees:
                result[name] = float(angle)*180/math.pi
            else:
                item = self.calibration[name]
                span = (item["range_max"]-item["range_min"])*2*math.pi/4095
                result[name] = float(angle)/span*200
        return result

    def to_si(self, values):
        result = []
        for name in self.joints:
            value = values[name]
            if name == "gripper":
                closed, opened = self.gripper["closed_rad"], self.gripper["open_rad"]
                result.append(closed+value/100*(opened-closed))
            elif self.use_degrees:
                result.append(value*math.pi/180)
            else:
                item = self.calibration[name]
                span = (item["range_max"]-item["range_min"])*2*math.pi/4095
                result.append(value/200*span)
        return np.asarray(result, dtype=float)

    def normalize(self, raw):
        result = {}
        for name in self.joints:
            item = self.calibration[name]
            lo, hi = item["range_min"], item["range_max"]
            val = raw[name]
            bounded = min(hi, max(lo, val))
            if name == "gripper":
                result[name] = (bounded-lo)/(hi-lo)*100
            elif self.use_degrees:
                result[name] = (val-(lo+hi)/2)*360/4095
            else:
                result[name] = (bounded-lo)/(hi-lo)*200-100
        return result

    def unnormalize(self, values):
        result = {}
        for name in self.joints:
            item = self.calibration[name]
            lo, hi = item["range_min"], item["range_max"]
            val = values[name]
            if not math.isfinite(val):
                raise ValueError("Nonfinite LeRobot action")
            if name == "gripper":
                tick = int(min(100., max(0., val))/100*(hi-lo)+lo)
            elif self.use_degrees:
                tick = int(val*4095/360+(lo+hi)/2)
            else:
                tick = int((min(100., max(-100., val))+100)/200*(hi-lo)+lo)
            # LeRobot encodes Goal_Position as signed magnitude (bit 15).
            if abs(tick) >= 1 << 15:
                raise ValueError("Goal exceeds signed servo register")
            result[name] = tick
        return result

    def relative_goal(self, target, present, maximum):
        if maximum is None:
            return np.asarray(target, dtype=float).copy()
        goals, positions = self.to_native(target), self.to_native(present)
        if isinstance(maximum, (float, int)) and not isinstance(maximum, bool):
            caps = dict.fromkeys(self.joints, maximum)
        elif isinstance(maximum, dict) and set(maximum) == set(self.joints):
            caps = maximum
        else:
            raise ValueError("max_relative_target must be a scalar or cover all joints")
        for name, cap in caps.items():
            if not math.isfinite(cap) or cap <= 0:
                raise ValueError("max_relative_target must be positive and finite")
            delta = min(cap, max(-cap, goals[name]-positions[name]))
            goals[name] = positions[name]+delta
        return self.to_si(goals)

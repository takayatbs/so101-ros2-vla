#!/usr/bin/env python3
"""Read-only servo/camera acceptance probe. Never writes torque or goal registers."""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import cv2
import numpy as np
from so101_core.config import load_system, resolve
from so101_core.hardware import SO101Bus
from so101_core.lerobot_compat import bus_options, control_profile


def probe_robot(role, config, samples):
    robot, adapter = config["robot"], None
    spec = robot[role]
    result = {"role": role, "port": spec["port"], "read_only": True}
    try:
        adapter = SO101Bus(spec["port"], resolve(robot, spec["calibration"]),
                           robot["joints"], spec["gripper"], motors=spec["motors"],
                           baudrate=spec["baudrate"], timeout_s=spec["serial_timeout_s"], read_only=True,
                           **bus_options(robot, role))
        result["calibration_matches"] = adapter.calibration_matches()
        durations, q = [], None
        for _ in range(samples):
            started = time.perf_counter()
            q = adapter.read()
            durations.append(time.perf_counter()-started)
        result.update(joint_positions_rad=dict(zip(robot["joints"], q.tolist(), strict=True)),
                      read_mean_ms=float(np.mean(durations)*1e3), read_p95_ms=float(np.percentile(durations, 95)*1e3),
                      torque_enabled=adapter.bus.read_all("Torque_Enable"), measurements=adapter.diagnostics())
        if role == "follower":
            result["control_profile"] = control_profile(robot)
            result["position_limits_enforced_in_software"] = {
                j: control_profile(robot) == "bounded" or j == "gripper" or not spec.get("use_degrees", True)
                for j in robot["joints"]}
            # SI conversion of an encoder endpoint may differ by a few ulps.
            def inside(joint, value):
                bounds = robot["limits"][joint]
                return bounds["min"]-1e-9 <= value <= bounds["max"]+1e-9
            result["inside_configured_limits"] = all(inside(j, value) for j, value in zip(robot["joints"], q, strict=True))
            result["outside_configured_limits"] = {
                j: {"position_rad": float(value), "min": robot["limits"][j]["min"], "max": robot["limits"][j]["max"]}
                for j, value in zip(robot["joints"], q, strict=True)
                if not inside(j, value)}
    except Exception as error:
        result["error"] = str(error)
    finally:
        if adapter is not None:
            adapter.close()
    return result


def probe_camera(name, config, samples):
    spec = config["camera"]["cameras"][name]
    cap = cv2.VideoCapture(spec["device"], cv2.CAP_V4L2)
    try:
        if spec.get("fourcc") is not None:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*spec["fourcc"]))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, spec["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, spec["height"])
        cap.set(cv2.CAP_PROP_FPS, spec["fps"])
        # Match runtime capture: one V4L2 buffer halves FPS on these UVC cameras.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
        warmup_deadline = time.monotonic()+spec.get("warmup_s", 1.)
        while time.monotonic() < warmup_deadline:
            if not cap.read()[0]:
                raise RuntimeError("Camera warmup failed")
        times, shape = [], None
        for _ in range(samples):
            ok, image = cap.read()
            if not ok:
                raise RuntimeError("Frame capture failed")
            shape = list(image.shape)
            times.append(time.monotonic())
        return {"camera": name, "device": spec["device"], "shape": shape,
                "actual_fps": (samples-1)/(times[-1]-times[0]), "read_only": True}
    except Exception as error:
        return {"camera": name, "device": spec["device"], "error": str(error)}
    finally:
        cap.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/workspace/config/runtime/system.yaml")
    parser.add_argument("--output", default="/workspace/data/logs/hardware_probe.json")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--cameras-only", action="store_true")
    parser.add_argument("--capture-width", type=int)
    parser.add_argument("--capture-height", type=int)
    args = parser.parse_args()
    if args.samples < 2:
        raise ValueError("At least two samples required")
    config = load_system(args.config)
    if bool(args.capture_width) != bool(args.capture_height):
        raise ValueError("Supply both --capture-width and --capture-height")
    if args.capture_width:
        if min(args.capture_width, args.capture_height) <= 0:
            raise ValueError("Capture dimensions must be positive")
        for spec in config["camera"]["cameras"].values():
            spec.update(width=args.capture_width, height=args.capture_height)
    result = {"timestamp": time.time(), "robots": [], "cameras": []}
    if not args.cameras_only:
        result["robots"] = [probe_robot(role, config, args.samples) for role in ("leader", "follower")]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(probe_camera, name, config, args.samples) for name in config["camera"]["cameras"]]
        result["cameras"] = [f.result() for f in futures]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    if any("error" in item for item in result["robots"]+result["cameras"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

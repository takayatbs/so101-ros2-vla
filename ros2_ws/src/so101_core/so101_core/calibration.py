"""Interactive project calibration, motor setup, and offline configuration updates."""
import argparse
import copy
import json
import math
import os
import select
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import yaml
from .config import load_system, resolve, local_config_path
from .feetech import ServoBus, TICK_RAD, BAUD_CODES
from .hardware import validate_calibration
from .lerobot_compat import control_profile


def clean(config):
    return {k: v for k, v in config.items() if not k.startswith("_")}


def backup(config, extra=()):
    robot = config["robot"]
    root = Path(robot.get("_config_root", robot["_root"])).parent / "data/calibration"
    folder = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    paths = [Path(robot["_file"]), Path(config["teleop"]["_file"]),
             *[resolve(robot, robot[r]["calibration"]) for r in ("leader", "follower")], *extra]
    for index, path in enumerate(paths):
        if path.is_file():
            shutil.copy2(path, folder / f"{index}-{path.name}")
    print(f"バックアップ: {folder}", flush=True)
    return folder


def commit_files(files):
    """Atomic replacement per file, rollback earlier replacements on failure."""
    originals, temps, replaced = {}, {}, []
    try:
        for path, content in files.items():
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            originals[path] = path.read_bytes() if path.exists() else None
            temp = path.with_name(path.name+".partial-"+uuid.uuid4().hex)
            temps[path] = temp
            temp.write_text(content)
            # Compose runs as root. Preserve the host user's ownership so
            # calibration/apply do not make existing YAML files uneditable.
            ownership = path.stat() if path.exists() else path.parent.stat()
            if path.exists():
                temp.chmod(ownership.st_mode & 0o777)
            if os.geteuid() == 0:
                os.chown(temp, ownership.st_uid, ownership.st_gid)
        for path, temp in temps.items():
            temp.replace(path)
            replaced.append(path)
    except BaseException:
        for path in reversed(replaced):
            if originals[path] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(originals[path])
        raise
    finally:
        for temp in temps.values():
            temp.unlink(missing_ok=True)


def settings_from_calibration(config):
    robot, teleop = copy.deepcopy(config["robot"]), copy.deepcopy(config["teleop"])
    spans, calibrations = {}, {}
    for role in ("leader", "follower"):
        spec = robot[role]
        raw = validate_calibration(json.loads(resolve(robot, spec["calibration"]).read_text()),
                                   robot["joints"], spec["motors"])
        calibrations[role] = raw
        spans[role] = (raw["gripper"]["range_max"]-raw["gripper"]["range_min"])*TICK_RAD
        spec["gripper"]["open_rad"] = spec["gripper"]["closed_rad"]+spans[role]
    follower, leader = robot["follower"]["gripper"], robot["leader"]["gripper"]
    robot["limits"]["gripper"].update(min=follower["closed_rad"], max=follower["open_rad"])
    scale = spans["follower"]/spans["leader"]
    teleop["joint_mapping"]["leader_gripper"].update(
        scale=scale, offset=follower["closed_rad"]-leader["closed_rad"]*scale)
    if control_profile(robot) == "lerobot":
        for joint in robot["joints"]:
            if joint == "gripper":
                continue
            span = (calibrations["follower"][joint]["range_max"]-
                    calibrations["follower"][joint]["range_min"])*TICK_RAD
            # Informational/URDF bounds; degree commands are not clipped to these.
            robot["limits"][joint].update(min=-span/2, max=span/2)
            native_per_rad = {}
            for role in ("leader", "follower"):
                item = calibrations[role][joint]
                width = (item["range_max"]-item["range_min"])*TICK_RAD
                native_per_rad[role] = 180/math.pi if robot[role].get("use_degrees", True) else 200/width
            teleop["joint_mapping"]["leader_"+joint].update(
                scale=native_per_rad["leader"]/native_per_rad["follower"], offset=0.)
    robot["hardware_qualified"] = False
    return robot, teleop, spans


def apply(config):
    robot, teleop, spans = settings_from_calibration(config)
    folder = backup(config)
    robot_path = local_config_path(robot['_file'])
    teleop_path = local_config_path(teleop['_file'])
    commit_files({robot_path: yaml.safe_dump(clean(robot), sort_keys=False, allow_unicode=True),
                  teleop_path: yaml.safe_dump(clean(teleop), sort_keys=False, allow_unicode=True)})
    config['robot']['_file'] = str(robot_path)
    config['teleop']['_file'] = str(teleop_path)
    report = {"spans_rad": spans, "gripper_mapping": teleop["joint_mapping"]["leader_gripper"],
              "hardware_qualified": False}
    (folder / "applied.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print("JSONからYAMLへ反映しました。Probeと実機受入を行ってください。")


def check(config):
    robot, teleop, spans = settings_from_calibration(config)
    for role in ("leader", "follower"):
        for key in ("closed_rad", "open_rad"):
            if not math.isclose(robot[role]["gripper"][key], config["robot"][role]["gripper"][key], rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("JSONとYAMLが一致しません。./scripts/calibrate.sh apply を実行してください")
    for key in ("min", "max"):
        if not math.isclose(robot["limits"]["gripper"][key], config["robot"]["limits"]["gripper"][key], rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("Gripper limits differ; run calibrate.sh apply")
    for key in ("scale", "offset"):
        if not math.isclose(teleop["joint_mapping"]["leader_gripper"][key], config["teleop"]["joint_mapping"]["leader_gripper"][key], rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("Gripper mapping differs; run calibrate.sh apply")
    for role in ("leader", "follower"):
        ServoBus(robot[role]["port"], robot[role]["motors"], robot[role]["baudrate"], robot[role]["serial_timeout_s"])
    if control_profile(robot) == "lerobot":
        for joint in robot["joints"]:
            for key in ("min", "max"):
                if not math.isclose(robot["limits"][joint][key], config["robot"]["limits"][joint][key], abs_tol=1e-9):
                    raise ValueError("Calibration-derived ranges differ; run calibrate.sh apply")
            for key in ("scale", "offset"):
                if not math.isclose(teleop["joint_mapping"]["leader_"+joint][key],
                                    config["teleop"]["joint_mapping"]["leader_"+joint][key], abs_tol=1e-9):
                    raise ValueError("LeRobot joint mapping differs; run calibrate.sh apply")
    print(json.dumps({"settings_match": True, "spans_rad": spans, "serial_access": False}, indent=2))


def invalidate(config):
    robot = config["robot"]
    robot["hardware_qualified"] = False
    path = local_config_path(robot['_file'])
    commit_files({path: yaml.safe_dump(clean(robot), sort_keys=False, allow_unicode=True)})
    robot['_file'] = str(path)


def make_bus(config, role):
    spec = config["robot"][role]
    if set(spec["motors"]) != set(config["robot"]["joints"]):
        raise ValueError("motors must contain the configured six joints")
    return ServoBus(spec["port"], spec["motors"], spec["baudrate"], spec["serial_timeout_s"])


def inspect_arm(config, role):
    """Read actual servo IDs/settings without requiring a calibration JSON."""
    bus = make_bus(config, role)
    print(f"読み取り対象: {role}, port={bus.port}, baudrate={bus.baudrate}", flush=True)
    result = {"role": role, "port": bus.port, "read_only": True, "motors": {}}
    try:
        bus.connect()
        for name, expected_id in bus.motors.items():
            values = {"configured_id": expected_id,
                      "id": bus.read("ID", name),
                      "baud_code": bus.read("Baud_Rate", name),
                      "model_number": bus.read("Model_Number", name),
                      "torque_enable": bus.read("Torque_Enable", name),
                      "position_tick": bus.read("Present_Position", name),
                      "phase": bus.read("Phase", name),
                      "operating_mode": bus.read("Operating_Mode", name)}
            print(f"{name}: {json.dumps(values)}", flush=True)
            if values["id"] != expected_id or values["baud_code"] != BAUD_CODES[bus.baudrate]:
                raise ValueError(f"Servo ID/baud settings mismatch: {name}")
            result["motors"][name] = values
        print("6サーボのID・通信速度を読み取りました。トルク・目標位置・校正値の書き込みは行っていません。")
        return result
    finally:
        bus.close()


def capture_range(bus, name):
    print(f"{name}: 手で安全な全範囲へゆっくり動かし、測定後Enterを押してください。", flush=True)
    low, high, count = 4095, 0, 0
    while True:
        tick = bus.read("Present_Position", name)
        if not 0 <= tick <= 4095:
            raise ValueError(f"{name}: 1回転範囲外の位置")
        low, high, count = min(low, tick), max(high, tick), count+1
        print(f"\r{name}: 現在={tick:4d}  最小={low:4d}  最大={high:4d}", end="", flush=True)
        if select.select([sys.stdin], [], [], .05)[0]:
            if sys.stdin.readline() == "":
                raise EOFError("Terminal input closed")
            print()
            break
    if count < 2 or high-low < 64 or high-low > 4000:
        raise ValueError(f"{name}: 測定範囲が小さすぎるか、回転境界を跨いでいます")
    return low, high


def measure_on_bus(bus, input_fn=input, capture_fn=capture_range):
    """Never enables torque or writes a target. Caller owns backup/rollback."""
    bus.disable_torque()
    for name in bus.motors:
        bus.write("Lock", name, 0)
        bus.write("Operating_Mode", name, 0)
        bus.write("Phase", name, bus.read("Phase", name) & ~0x10)
    input_fn("全関節を可動範囲の中央付近へ。wrist_rollも中立向き、gripperは中間開度。支持してEnter: ")
    for name in bus.motors:
        for register, value in [("Homing_Offset", 0), ("Min_Position_Limit", 0), ("Max_Position_Limit", 4095)]:
            bus.write(register, name, value)
    positions = bus.read_all("Present_Position")
    raw = {}
    for name, tick in positions.items():
        offset = tick-2047
        if not 0 <= tick <= 4094:
            raise ValueError(f"{name}: 原点が境界に近すぎます。中央姿勢を調整してください")
        bus.write("Homing_Offset", name, offset)
        raw[name] = {"id": bus.motors[name], "drive_mode": 0, "homing_offset": offset,
                     "range_min": 0, "range_max": 4095}
    for name in bus.motors:
        if name == "wrist_roll":
            continue
        if name == "gripper":
            input_fn("gripperを無理なく閉じた位置に手で合わせてEnter: ")
            low = bus.read("Present_Position", name)
            input_fn("gripperを無理なく開いた位置に手で合わせてEnter: ")
            high = bus.read("Present_Position", name)
            if not 0 <= low < high <= 4095 or high-low < 64:
                raise ValueError("gripper: 閉位置 < 開位置、かつ64tick以上が必要。組立と方向を確認してください")
        else:
            low, high = capture_fn(bus, name)
        raw[name].update(range_min=low, range_max=high)
    validate_calibration(raw, bus.motors, bus.motors)
    bus.write_calibration(raw)
    return raw


def measure(config, role):
    if not sys.stdin.isatty():
        raise ValueError("対話ターミナルが必要です。ホストからcalibrate.shを実行してください")
    bus = make_bus(config, role)
    print(f"対象: {role}, port={bus.port}, IDs={bus.motors}")
    if input("電源・支持・停止手段を確認し、このアームを測定する場合 yes を入力: ").strip() != "yes":
        raise ValueError("校正を中止しました（接続・書き込みなし）")
    folder = backup(config)
    snapshot = None
    try:
        bus.connect()
        snapshot = {"calibration": bus.read_calibration(),
                    "phase": bus.read_all("Phase"), "mode": bus.read_all("Operating_Mode")}
        (folder / f"{role}-servo-before.json").write_text(json.dumps(snapshot, indent=2))
        invalidate(config)
        try:
            raw = measure_on_bus(bus)
            path = resolve(config["robot"], config["robot"][role]["calibration"])
            (folder / f"{role}-measurement.json").write_text(json.dumps({"role": role, "port": bus.port,
                "measured_at": datetime.now(timezone.utc).isoformat(), "calibration": raw}, indent=2))
            commit_files({path: json.dumps(raw, indent=2)+"\n"})
        except BaseException:
            print("測定を中止。トルクOFFのまま元の校正・モードを復元します。", file=sys.stderr)
            try:
                bus.disable_torque()
                bus.write_calibration(snapshot["calibration"])
                for name in bus.motors:
                    bus.write("Phase", name, snapshot["phase"][name])
                    bus.write("Operating_Mode", name, snapshot["mode"][name])
            except Exception as error:
                print(f"復元に失敗: {error}。電源・配線を確認し、{folder} の記録から復旧してください。", file=sys.stderr)
            raise
        print(f"保存しました: {path}\n両アームの測定後 ./scripts/calibrate.sh apply を実行してください。")
    finally:
        if bus.is_connected:
            try:
                bus.disable_torque()
            finally:
                bus.close()


def setup_motor(config, role, joint, current_id, current_baud):
    spec = config["robot"][role]
    target_id, target_baud = spec["motors"][joint], spec["baudrate"]
    # Duplicate IDs on a chain cannot be detected reliably by broadcast scanning.
    print(f"対象: {role}/{joint}: ID {current_id} -> {target_id}, baud {current_baud} -> {target_baud}")
    if input("設定対象のサーボ1台だけを接続してください。確認後 ONE を入力: ").strip() != "ONE":
        raise ValueError("中止しました（接続・書き込みなし）")
    if target_baud not in BAUD_CODES:
        raise ValueError("Unsupported target baudrate")
    bus = ServoBus(spec["port"], {joint: current_id}, current_baud, spec["serial_timeout_s"])
    backup(config)
    invalidate(config)
    try:
        bus.connect()
        print("Servo model number:", bus.read("Model_Number", joint))
        bus.disable_torque()
        bus.write("Lock", joint, 0)
        if current_id != target_id:
            # A status reply may already carry the new ID/baud. Verify using
            # the new settings rather than requiring an old-address ACK.
            bus.write("ID", joint, target_id, expect_reply=False)
            time.sleep(.05)
            bus.motors[joint] = target_id
        if bus.read("ID", joint) != target_id:
            raise IOError("Motor ID read-back mismatch")
        if current_baud != target_baud:
            bus.write("Baud_Rate", joint, BAUD_CODES[target_baud], expect_reply=False)
            bus.close()
            bus.baudrate = target_baud
            time.sleep(.1)
            bus.connect()
        if bus.read("ID", joint) != target_id or bus.read("Baud_Rate", joint) != BAUD_CODES[target_baud]:
            raise IOError("Motor setup read-back mismatch")
        bus.disable_torque()
        print("IDと通信速度を確認しました。全6台の設定・配線後に校正してください。")
    finally:
        bus.close()


def restore(config, role, snapshot_path):
    snapshot = json.loads(Path(snapshot_path).read_text())
    raw = validate_calibration(snapshot["calibration"], config["robot"]["joints"], config["robot"][role]["motors"])
    for key, maximum in [("phase", 255), ("mode", 3)]:
        if set(snapshot[key]) != set(raw) or any(type(v) is not int or not 0 <= v <= maximum for v in snapshot[key].values()):
            raise ValueError("Invalid servo snapshot: "+key)
    bus = make_bus(config, role)
    print(f"復元対象: {role}, port={bus.port}, snapshot={snapshot_path}")
    if input("対象のアームを支持し、保存したサーボ値を復元する場合 yes を入力: ").strip() != "yes":
        raise ValueError("復元を中止しました（接続・書き込みなし）")
    backup(config, [Path(snapshot_path)])
    invalidate(config)
    try:
        bus.connect()
        bus.disable_torque()
        bus.write_calibration(raw)
        for name in bus.motors:
            bus.write("Phase", name, snapshot["phase"][name])
            bus.write("Operating_Mode", name, snapshot["mode"][name])
            if bus.read("Phase", name) != snapshot["phase"][name] or bus.read("Operating_Mode", name) != snapshot["mode"][name]:
                raise IOError("Restored servo mode/phase read-back mismatch")
        path = resolve(config["robot"], config["robot"][role]["calibration"])
        commit_files({path: json.dumps(raw, indent=2)+"\n"})
        print("サーボとJSONを復元しました。apply・Probe・実機受入を再実施してください。")
    finally:
        if bus.is_connected:
            try:
                bus.disable_torque()
            finally:
                bus.close()


def main():
    parser = argparse.ArgumentParser(description="SO101: 校正・設定をこのプロジェクト内で管理")
    parser.add_argument("--config", default="/workspace/config/runtime/system.real30hz.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="JSONとYAMLの整合性を確認（実機へ接続しない）")
    sub.add_parser("apply", help="校正JSONからグリッパー設定を更新（実機へ接続しない）")
    inspect_parser = sub.add_parser("inspect", help="指定アームのID・位置・トルクを実機から読み取る（書き込みなし）")
    inspect_parser.add_argument("role", choices=["leader", "follower"])
    measure_parser = sub.add_parser("measure", help="対話形式で原点と可動範囲を測定")
    measure_parser.add_argument("role", choices=["leader", "follower"])
    setup = sub.add_parser("setup-motor", help="1台のみ接続してID/baudを設定")
    setup.add_argument("role", choices=["leader", "follower"])
    setup.add_argument("joint")
    setup.add_argument("--current-id", type=int, required=True)
    setup.add_argument("--current-baud", type=int, required=True)
    restore_parser = sub.add_parser("restore", help="通信復旧後にサーボとJSONをバックアップから復元")
    restore_parser.add_argument("role", choices=["leader", "follower"])
    restore_parser.add_argument("--snapshot", required=True)
    args = parser.parse_args()
    config = load_system(args.config)
    if args.command == "measure":
        measure(config, args.role)
    elif args.command == "inspect":
        inspect_arm(config, args.role)
    elif args.command == "setup-motor":
        setup_motor(config, args.role, args.joint, args.current_id, args.current_baud)
    elif args.command == "restore":
        restore(config, args.role, args.snapshot)
    elif args.command == "apply":
        apply(config)
    else:
        check(config)


if __name__ == "__main__":
    main()

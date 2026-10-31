import shutil
import json
import os
import signal
import subprocess
import time
from pathlib import Path
import numpy as np
import pytest
import yaml

rclpy = pytest.importorskip("rclpy")
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import JointState
from std_msgs.msg import String, Bool
from std_srvs.srv import Trigger
from trajectory_msgs.msg import JointTrajectory
from diagnostic_msgs.msg import DiagnosticArray
from so101_msgs.srv import SetControlMode, StartEpisode, StopEpisode
from so101_core.ros_utils import LATCHED_QOS, COMMAND_QOS, trajectory
from so101_core.config import load_system
from so101_core.converter import convert


@pytest.fixture
def runtime(tmp_path, request):
    options = getattr(request, "param", {})
    source = Path(__file__).resolve().parents[1]/"config"
    folder = tmp_path/"config"
    folder.mkdir()
    shutil.copytree(source, folder, dirs_exist_ok=True)
    manifest_path = folder/options.get('profile', 'runtime/system.yaml')
    manifest = yaml.safe_load(manifest_path.read_text())
    camera_path = manifest_path.parent/manifest['files']['camera']
    camera = yaml.safe_load(camera_path.read_text())
    for spec in camera["cameras"].values():
        spec.update(width=options.get("width", 160), height=options.get("height", 120))
    camera_path.write_text(yaml.safe_dump(camera))
    obs = yaml.safe_load((folder/"observation/observation.yaml").read_text())
    for spec in obs["preprocessing"].values():
        spec["resize"].update(width=64, height=48)
    (folder/"observation/observation.yaml").write_text(yaml.safe_dump(obs))
    log = (tmp_path/"runtime.log").open("w")
    # Isolated DDS domain, unrelated to any connected physical runtime.
    os.environ["ROS_DOMAIN_ID"] = str(100+os.getpid()%100)
    rclpy.init()
    node = rclpy.create_node("integration_test")
    state = {"mode": None, "q": None, "enabled": False}
    node.create_subscription(String, "/control_mode", lambda msg: state.update(mode=msg.data), LATCHED_QOS)
    node.create_subscription(JointState, "/follower/joint_states", lambda msg: state.update(q=msg.position), qos_profile_sensor_data)
    node.create_subscription(Bool, "/hardware/motor_enable", lambda msg: state.update(enabled=msg.data), COMMAND_QOS)
    proc = subprocess.Popen(["ros2", "launch", "so101_bringup", "runtime.launch.py", "backend:=mock",
                             "foxglove:=false", f"config:={folder/options.get('profile', 'runtime/system.yaml')}"], stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    def spin_until(predicate, timeout=15):
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError((tmp_path/"runtime.log").read_text())
            rclpy.spin_once(node, timeout_sec=.02)
        assert predicate(), (tmp_path/"runtime.log").read_text()[-6000:]
    def call(service_type, name, **kwargs):
        client = node.create_client(service_type, name)
        assert client.wait_for_service(timeout_sec=10), name
        future = client.call_async(service_type.Request(**kwargs))
        spin_until(future.done)
        response = future.result()
        node.destroy_client(client)
        return response
    try:
        spin_until(lambda: state["mode"] == "IDLE" and state["q"] is not None)
        # Allow both controller spawners to finish before arming the stream.
        from controller_manager_msgs.srv import ListControllers
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            controllers = call(ListControllers, "/controller_manager/list_controllers").controller
            if len(controllers) == 2 and all(c.state == "active" for c in controllers):
                break
        else:
            raise AssertionError("Controller activation failed")
        yield node, state, call, spin_until, folder, tmp_path, proc
    finally:
        node.destroy_node()
        if rclpy.ok(): rclpy.shutdown()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGINT)
            try: proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        log.close()


@pytest.mark.ros
def test_lerobot_forward_controller_preserves_goal_without_extra_motion_limits(runtime):
    node, state, call, spin_until, folder, tmp, proc = runtime
    received = {"safe": None, "hardware": None}
    node.create_subscription(JointTrajectory, "/safe_action",
                             lambda msg: received.update(safe=list(msg.points[0].positions)), COMMAND_QOS)
    node.create_subscription(JointState, "/hardware/follower/command",
                             lambda msg: received.update(hardware=list(msg.position)), COMMAND_QOS)
    publisher = node.create_publisher(JointTrajectory, "/moveit/action", COMMAND_QOS)
    assert call(SetControlMode, "/set_control_mode", mode="MOVEIT").success
    target = [.8, 2.1, -2., -.5, 3., 1.]
    config = load_system(folder/"runtime/system.yaml")
    timer = node.create_timer(.02, lambda: publisher.publish(trajectory(node, config["robot"]["joints"], target)))
    try:
        spin_until(lambda: received["hardware"] is not None and np.allclose(received["hardware"], target), timeout=10)
        np.testing.assert_allclose(received["safe"], target)
        assert state["mode"] == "MOVEIT"
    finally:
        node.destroy_timer(timer)


@pytest.mark.ros
def test_launch_teleop_mcap_and_latched_stops(runtime):
    node, state, call, spin_until, folder, tmp, proc = runtime
    assert call(SetControlMode, "/set_control_mode", mode="TELEOP").success
    spin_until(lambda: state["enabled"] and np.linalg.norm(state["q"]) > .02)
    started = call(StartEpisode, "/start_episode", task_id="Move test arm", operator_name="pytest")
    assert started.success, started.message
    deadline = time.monotonic()+1.0
    while time.monotonic() < deadline: rclpy.spin_once(node, timeout_sec=.02)
    assert call(StopEpisode, "/stop_episode", success=True, failure_reason="").success
    episode = tmp/"data/bags"/started.episode_id
    metadata = json.loads((episode/"episode.json").read_text())
    assert metadata["status"] == "complete" and metadata["success"]
    assert list((episode/"raw").glob("*.mcap"))
    result = convert([episode], tmp/"data/datasets/smoke_v1", "local/so101-smoke", load_system(folder/"runtime/system.yaml"))
    assert result["episodes"][0]["frames"] > 10
    assert result["action_semantics"] == "absolute actually submitted joint position"
    # A changed calibration must not silently relabel recorded joint positions.
    original = (episode/"episode.json").read_text()
    metadata["calibration_sha256"] = {"leader": "changed-after-recording"}
    try:
        (episode/"episode.json").write_text(json.dumps(metadata))
        with pytest.raises(ValueError, match="Calibration differs"):
            convert([episode], tmp/"data/datasets/rejected", "invalid", load_system(folder/"runtime/system.yaml"))
        assert not (tmp/"data/datasets/rejected").exists()
    finally:
        (episode/"episode.json").write_text(original)
    assert call(Trigger, "/software_estop").success
    spin_until(lambda: state["mode"] == "ESTOP" and not state["enabled"])
    assert not call(SetControlMode, "/set_control_mode", mode="TELEOP").success
    assert call(Trigger, "/reset_fault").success
    spin_until(lambda: state["mode"] == "IDLE" and not state["enabled"])
    # Independent source with no publisher heartbeat must fault after timeout.
    pub = node.create_publisher(JointTrajectory, "/moveit/action", COMMAND_QOS)
    assert call(SetControlMode, "/set_control_mode", mode="MOVEIT").success
    deadline = time.monotonic()+.5
    config = load_system(folder/"runtime/system.yaml")
    while time.monotonic() < deadline:
        pub.publish(trajectory(node, config["robot"]["joints"], [0]*5+[.5]))
        rclpy.spin_once(node, timeout_sec=.01)
    spin_until(lambda: state["mode"] == "FAULT" and not state["enabled"])
    assert call(Trigger, "/reset_fault").success
    assert call(SetControlMode, "/set_control_mode", mode="MOVEIT").success
    msg = trajectory(node, config["robot"]["joints"], [float('nan')]*6)
    pub.publish(msg)
    spin_until(lambda: state["mode"] == "FAULT" and not state["enabled"])


@pytest.mark.ros
def test_fault_reason_reaches_late_diagnostic_subscriber(runtime):
    node, state, call, spin_until, folder, tmp, proc = runtime
    assert call(SetControlMode, "/set_control_mode", mode="MOVEIT").success
    spin_until(lambda: state["mode"] == "FAULT" and not state["enabled"])
    # Attach only after the original error message has already been published.
    reasons = []
    node.create_subscription(DiagnosticArray, "/diagnostics",
        lambda msg: reasons.extend(s.message for s in msg.status if s.name == "safety_node"), 10)
    spin_until(lambda: "Command source did not start" in reasons, timeout=5)
    assert call(Trigger, "/reset_fault").success
    spin_until(lambda: state["mode"] == "IDLE")


@pytest.mark.ros
@pytest.mark.soak
@pytest.mark.skipif(os.environ.get("RUN_SOAK_TESTS") != "1", reason="Set RUN_SOAK_TESTS=1")
@pytest.mark.parametrize("runtime", [{"width": 1920, "height": 1080, "profile": "runtime/system.real30hz.yaml"}], indirect=True)
def test_1080p_30hz_teleop_stays_active(runtime):
    node, state, call, spin_until, folder, tmp, proc = runtime
    assert call(SetControlMode, "/set_control_mode", mode="TELEOP").success
    spin_until(lambda: state["enabled"] and np.linalg.norm(state["q"]) > .02)
    # The reported failure occurred after ~80 seconds with full-resolution
    # image traffic. Keep all camera/recorder subscriptions active for longer.
    deadline = time.monotonic()+120
    while time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=.02)
        assert proc.poll() is None, (tmp/"runtime.log").read_text()[-6000:]
        assert state["mode"] == "TELEOP", (tmp/"runtime.log").read_text()[-6000:]
    # Also exercise real MCAP I/O under that image/control traffic.
    started = call(StartEpisode, "/start_episode", task_id="30 Hz soak", operator_name="pytest")
    assert started.success, started.message
    deadline = time.monotonic()+2
    while time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=.02)
        assert state["mode"] == "TELEOP"
    assert call(StopEpisode, "/stop_episode", success=True, failure_reason="").success
    episode = tmp/"data/bags"/started.episode_id
    assert json.loads((episode/"episode.json").read_text())["status"] == "complete"
    assert list((episode/"raw").glob("*.mcap"))
    from controller_manager_msgs.srv import ListControllers
    controllers = call(ListControllers, "/controller_manager/list_controllers").controller
    assert len(controllers) == 2 and all(c.state == "active" for c in controllers)
    assert call(SetControlMode, "/set_control_mode", mode="IDLE").success

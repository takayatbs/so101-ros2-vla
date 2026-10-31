import shutil
import json
import os
import subprocess
import sys
from pathlib import Path
import numpy as np
import pytest
import yaml
from so101_core.config import load_system, config_hash, calibration_hashes

pytestmark = [pytest.mark.ml, pytest.mark.skipif(os.environ.get("RUN_ML_TESTS") != "1", reason="Set RUN_ML_TESTS=1")]


@pytest.mark.parametrize('smoothing', ['none', 'three_point'])
def test_act_training_registry_and_ros_inference(tmp_path, smoothing):
    from so101_core.dataset import DatasetWriter, FORMAT
    from so101_core.model_registry import register
    from so101_core.policy import ACTPolicy
    source = Path(__file__).resolve().parents[1]
    folder = tmp_path/"config"
    folder.mkdir()
    shutil.copytree((source/"config"), folder, dirs_exist_ok=True)
    obs = yaml.safe_load((folder/"observation/observation.yaml").read_text())
    for spec in obs["preprocessing"].values():
        spec["resize"].update(width=64, height=48)
    (folder/"observation/observation.yaml").write_text(yaml.safe_dump(obs))
    config = load_system(folder/"runtime/system.yaml")
    joints = config["robot"]["joints"]
    dataset_root = tmp_path/"data/datasets/synthetic_v1"
    dataset = DatasetWriter(dataset_root, "local/synthetic-smoke", 30, joints,
                            {name: (48,64,3) for name in obs["cameras"]})
    rng = np.random.default_rng(42)
    for episode in range(2):
        for i in range(20):
            q = np.array([.01*np.sin(i/5)+episode*.05]*5+[.5], dtype=np.float32)
            images = {name: rng.integers(0,255,(48,64,3),dtype=np.uint8) for name in obs["cameras"]}
            dataset.add_frame(q, q.copy(), images, i/30, "Synthetic smoke test")
        dataset.save_episode(f"synthetic-{episode}")
    dataset.finalize()
    provenance = {"dataset_name":"local/synthetic-smoke", "dataset_version":"synthetic_v1",
                  "observation_hash":config_hash(config["observation"]), "units":"rad", "joint_names":joints,
                  "robot_hash":config_hash(config["robot"]), "dataset_format_version":FORMAT,
                  "calibration_sha256":calibration_hashes(config["robot"])}
    (dataset_root/"provenance.json").write_text(json.dumps(provenance))
    output = tmp_path/"training"
    log = tmp_path/"act-smoke.log"
    command = [sys.executable, str(source/"training/train.py"), "--config",str(folder/"runtime/system.yaml"),
               "--dataset",str(dataset_root),"--output",str(output),"--model-id","smoke-act",
               "--steps","2","--batch-size","2","--device","cpu",
               "--dim-model", "32", "--n-heads", "4",
               "--dim-feedforward", "64", "--n-encoder-layers", "1", "--n-decoder-layers", "1",
               "--n-vae-encoder-layers", "1", "--num-workers", "0"]
    env = dict(os.environ, OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2")
    with log.open("w") as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, env=env, timeout=180)
    assert result.returncode == 0, log.read_text()[-7000:]
    model = tmp_path/"data/models/smoke-act"
    manifest = json.loads((model/"deployment.json").read_text())
    assert manifest["training_seed"] == 42 and manifest["units"] == "rad"
    assert manifest["training_config"]["train_episodes"] == [0]
    assert manifest["training_config"]["validation_episodes"] == [1]
    assert np.isfinite(manifest["metrics"]["validation_l1_rad"])
    stats = json.loads((model/"checkpoint/normalization.json").read_text())
    assert stats["state_mean"][0] < .02  # Held-out episode has an added .05 rad offset.
    adapter = ACTPolicy(str(model/"checkpoint"), device="cpu")
    observation = {"observation.state": np.array([0]*5+[.5],dtype=np.float32), "task":"Synthetic smoke test"}
    for name in obs["cameras"]:
        observation[f"observation.images.{name}"] = np.zeros((48,64,3),dtype=np.uint8)
    chunk = adapter.predict(observation)
    assert chunk.shape == (30,6) and np.isfinite(chunk).all()
    adapter.reset()
    # Test the actual ROS policy adapter without a physical robot.
    rclpy = pytest.importorskip("rclpy")
    from sensor_msgs.msg import Image, JointState
    from std_msgs.msg import Bool, String
    from trajectory_msgs.msg import JointTrajectory
    from so101_policy.node import PolicyNode
    from so101_core.ros_utils import STATE_QOS, COMMAND_QOS, LATCHED_QOS, joint_state
    from rclpy.executors import SingleThreadedExecutor
    import time
    os.environ["ROS_DOMAIN_ID"] = str(100+os.getpid()%100)
    # Policy config must use CPU as this test runs in CPU CI too.
    policy_config = yaml.safe_load((folder/"policy/policy.yaml").read_text())
    policy_config["device"] = "cpu"
    (folder/"policy/policy.yaml").write_text(yaml.safe_dump(policy_config))
    rclpy.init(args=["--ros-args", "-p", f"config:={folder/'runtime/system.yaml'}", "-p", "policy_backend:=act",
                     "-p", "model_id:=smoke-act", '-p', 'action_smoothing:='+smoothing])
    policy_node = PolicyNode()
    assert policy_node.scheduler.smoothing == smoothing
    publisher = rclpy.create_node("synthetic_sensors")
    executor = SingleThreadedExecutor()
    executor.add_node(policy_node); executor.add_node(publisher)
    actions = []
    readiness = []
    diagnostics = []
    from diagnostic_msgs.msg import DiagnosticArray
    publisher.create_subscription(DiagnosticArray, '/diagnostics',
                                  lambda msg: diagnostics.extend(s.message for s in msg.status if s.name == 'policy_node'), 10)
    publisher.create_subscription(JointTrajectory, "/policy/action", actions.append, COMMAND_QOS)
    publisher.create_subscription(Bool, "/policy/ready", lambda msg: readiness.append(msg.data), LATCHED_QOS)
    states = publisher.create_publisher(JointState, "/follower/joint_states", STATE_QOS)
    modes = publisher.create_publisher(String, "/control_mode", LATCHED_QOS)
    cameras = {name: publisher.create_publisher(Image, f"/camera/{name}/image_raw", STATE_QOS) for name in obs["cameras"]}
    try:
        deadline, last, last_state = time.monotonic()+15, 0.0, 0.0
        autonomous = False
        while not actions and time.monotonic() < deadline:
            # Production joints run at 100 Hz while cameras run at 30 Hz.
            if time.monotonic()-last_state >= .01:
                last_state = time.monotonic()
                states.publish(joint_state(publisher, joints, [0]*5+[.5]))
            if time.monotonic()-last >= .033:
                last = time.monotonic()
                stamp = publisher.get_clock().now().to_msg()
                if readiness and readiness[-1]:
                    if not autonomous:
                        assert not actions, "IDLE warm-up must never publish a robot command"
                    autonomous = True
                modes.publish(String(data="AUTONOMOUS" if autonomous else "IDLE"))
                for name, pub in cameras.items():
                    msg = Image(height=120,width=160,encoding="rgb8",step=480,data=bytes(120*480))
                    # Arrival/header phases differ from the newest joint state.
                    # The previous implementation rejected this forever at 30 Hz.
                    from builtin_interfaces.msg import Time
                    camera_ns = stamp.sec*10**9+stamp.nanosec-(28_000_000 if name == 'top' else 14_000_000)
                    msg.header.stamp = Time(sec=camera_ns//10**9, nanosec=camera_ns%10**9)
                    pub.publish(msg)
            executor.spin_once(timeout_sec=.005)
        assert actions, f"No policy action; readiness={readiness}, diagnostics={diagnostics[-5:]}"
        assert autonomous and True in readiness
        assert actions[0].joint_names == joints
        assert np.isfinite(actions[0].points[0].positions).all()
    finally:
        policy_node.destroy_node(); publisher.destroy_node(); executor.shutdown(); rclpy.shutdown()

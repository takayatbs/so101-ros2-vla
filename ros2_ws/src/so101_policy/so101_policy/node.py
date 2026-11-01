import json
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sensor_msgs.msg import Image, JointState
from std_msgs.msg import Bool, String
from trajectory_msgs.msg import JointTrajectory
from diagnostic_msgs.msg import DiagnosticStatus
from so101_core.config import resolve, config_hash
from so101_core.converter import sha256_file
from so101_core.control import reorder
from so101_core.policy import HoldPolicy, ACTPolicy, ChunkScheduler
from so101_core.preprocessing import ImagePreprocessor, image_from_ros
from so101_core.synchronization import alignment_settings
from so101_core.ros_utils import (SystemNode, STATE_QOS, COMMAND_QOS, LATCHED_QOS,
                                 seconds, trajectory, run)


class PolicyNode(SystemNode):
    def __init__(self):
        super().__init__("policy_node")
        self.declare_parameter("policy_backend", self.config["policy"]["backend"])
        self.declare_parameter("model_id", self.config["policy"]["model_id"])
        self.declare_parameter('correction_checkpoint','')
        self.declare_parameter('action_smoothing', 'none')
        self.declare_parameter('asynchronous_inference', False)
        self.spec = self.config["policy"]
        self.sync = alignment_settings(self.config['dataset'])
        backend = self.get_parameter("policy_backend").value
        self.use_camera_observations = backend in ('act','flow','lerobot') or not self.config["camera"].get("video", {}).get("enabled", False)
        self.model_id = self.get_parameter("model_id").value
        if backend in ('act','flow','lerobot'):
            root = resolve(self.spec, self.spec["model_root"])
            folder = (root/self.model_id).resolve()
            if not folder.is_relative_to(root.resolve()):
                raise ValueError("model_id must remain under model_root")
            manifest = json.loads((folder/"deployment.json").read_text())
            if manifest["observation_hash"] != config_hash(self.config["observation"]):
                raise ValueError("Checkpoint preprocessing/schema differs from runtime")
            if manifest["joint_names"] != self.joints or manifest["units"] != "rad":
                raise ValueError("Checkpoint joint order/units differ from runtime")
            for relative, expected in manifest["checkpoint_sha256"].items():
                path = (folder/"checkpoint"/relative).resolve()
                if not path.is_relative_to((folder/"checkpoint").resolve()) or sha256_file(path) != expected:
                    raise ValueError("Checkpoint hash mismatch: "+relative)
            if manifest.get("robot_hash") != config_hash(self.config["robot"]):
                raise ValueError("Checkpoint robot/calibration settings differ from runtime")
            if manifest.get('synchronization') and manifest['synchronization'] != self.sync:
                raise ValueError('Checkpoint/runtime timestamp synchronization settings differ')
            for role, expected in manifest.get("calibration_sha256", {}).items():
                if sha256_file(resolve(self.config["robot"], self.config["robot"][role]["calibration"])) != expected:
                    raise ValueError("Calibration changed since training: "+role)
            if backend in ('flow','lerobot'):
                from so101_core.method_training import load_method_policy
                self.policy = load_method_policy(folder/'checkpoint',self.spec['device'])
                policy_config = self.policy.config
            else:
                self.policy = ACTPolicy(str(folder/"checkpoint"), self.spec["device"])
                policy_config = self.policy.policy.config
            if policy_config["chunk_size"] != self.spec["action_chunk"]["chunk_size"]:
                raise ValueError("Checkpoint/runtime action chunk length differs")
            if backend in ('flow','lerobot'):
                method = self.spec['action_chunk']['scheduler']['method']
                required = 'standard' if method in ('standard','rtc') else method
                if policy_config['objective'] != required or policy_config['fps'] != self.spec['inference_rate_hz']:
                    raise ValueError('Checkpoint/runtime training objective or FPS differs')
        elif backend == "hold":
            self.policy = HoldPolicy()
        else:
            raise ValueError("Unknown policy backend")
        self.pre = ImagePreprocessor(self.config["observation"])
        chunk = self.spec["action_chunk"]
        smoothing = self.get_parameter('action_smoothing').value
        if smoothing not in ('none', 'three_point'):
            raise ValueError('Unknown action smoothing method')
        self.engine = None
        self.engine_failed = False
        if backend in ('flow','lerobot') and (chunk['scheduler']['method'] != 'standard'
                or chunk['correction']['method'] != 'none'
                or self.get_parameter('asynchronous_inference').value):
            from so101_core.async_chunks import AsyncChunkScheduler
            from so101_core.flow import A2C2
            correction = None
            if chunk['correction']['method'] == 'a2c2':
                path = self.get_parameter('correction_checkpoint').value or chunk['correction'].get('checkpoint','')
                if not path:raise ValueError('A trained A2C2 correction_checkpoint is required')
                correction = A2C2(resolve(self.spec,path),self.policy)
                if correction.config['method'] != chunk['scheduler']['method']:
                    raise ValueError('A2C2 cache/runtime scheduler mismatch')
            elif chunk['correction']['method'] != 'none':raise ValueError('Unknown correction method')
            scheduling = chunk['scheduler']
            self.engine = AsyncChunkScheduler(self.policy,scheduling['method'],self.spec['inference_rate_hz'],
                chunk['n_action_steps'],scheduling.get('max_delay_steps',3),scheduling.get('max_inference_s',.2),
                correction, smoothing=smoothing)
        self.scheduler = ChunkScheduler(chunk["n_action_steps"],
            'standard' if backend in ('hold','flow','lerobot') else chunk['scheduler']['method'],
            'none' if backend in ('hold','flow','lerobot') else chunk['correction']['method'],
            smoothing=smoothing, joint_names=self.joints)
        self.policy.reset()
        self.state, self.images, self.mode = None, {}, "IDLE"
        self.measured_state_received, self.state_source = None, None
        self.state_history = deque(maxlen=64)
        self.image_history = {name: deque(maxlen=3) for name in self.config["observation"]["cameras"]}
        self.epoch = 0
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="policy")
        self.future = None
        self.reset_pending = False
        self.ready = False
        self.job_warmup = False
        self.ready_pub = self.create_publisher(Bool, "/policy/ready", LATCHED_QOS)
        self.ready_pub.publish(Bool(data=False))
        self.pub = self.create_publisher(JointTrajectory, "/policy/action", COMMAND_QOS)
        self.measured_state_topic = self.config['dataset'].get('measurement_state_topic','/hardware/follower/state')
        self.create_subscription(JointState, self.measured_state_topic, self.receive_measured_state, STATE_QOS)
        if self.measured_state_topic != '/follower/joint_states':
            self.create_subscription(JointState, "/follower/joint_states", self.receive_controller_state, STATE_QOS)
        self.create_subscription(String, "/control_mode", self.receive_mode, LATCHED_QOS)
        for name in self.config["observation"]["cameras"] if self.use_camera_observations else []:
            self.create_subscription(Image, f"/camera/{name}/image_raw", lambda msg, n=name: self.receive_image(n, msg), STATE_QOS)
        self.create_timer(1/self.spec["inference_rate_hz"], self.tick)

    def receive_mode(self, msg):
        if msg.data != self.mode:
            self.epoch += 1
            self.reset_pending = True
            if self.engine is not None:self.engine.reset()
            if msg.data != 'AUTONOMOUS':self.engine_failed=False
        self.mode = msg.data

    def receive_state(self, msg):
        try:
            self.state = time.monotonic(), seconds(msg.header.stamp), reorder(msg.name, msg.position, self.joints)
            self.state_history.append(self.state)
        except ValueError as error:
            self.state = None
            self.state_history.clear()
            self.diagnostic(DiagnosticStatus.ERROR, str(error))

    def receive_measured_state(self, msg):
        self.measured_state_received = time.monotonic()
        if self.state_source != self.measured_state_topic:
            self.state_history.clear()
        self.state_source = self.measured_state_topic
        self.receive_state(msg)

    def receive_controller_state(self, msg):
        if self.measured_state_received is None or time.monotonic()-self.measured_state_received > self.config['observation']['max_age_s']:
            if self.state_source != '/follower/joint_states': self.state_history.clear()
            self.state_source = '/follower/joint_states'
            self.receive_state(msg)

    def receive_image(self, name, msg):
        self.images[name] = time.monotonic(), seconds(msg.header.stamp), msg
        self.image_history[name].append(self.images[name])

    def observation(self):
        spec, now = self.config["observation"], time.monotonic()
        if self.state is None or now-self.state[0] > spec["max_age_s"]:
            raise ValueError("Follower observation stale")
        if not self.use_camera_observations:
            # The hold backend only returns the current joints. During TELEOP
            # it does not need a second, uncompressed camera transport.
            return {"observation.state": self.state[2].copy(), "task": self.spec["task"]}
        clock_now = self.get_clock().now().nanoseconds/1e9
        latest = []
        for name in spec["cameras"]:
            sample = self.images.get(name)
            if sample is None or now-sample[0] > spec["max_age_s"]:
                raise ValueError("Camera observation stale: "+name)
            latest.append(sample[1])
        # Cameras and joint states arrive at different phases. Match their
        # timestamps instead of requiring the newest state to match an older image.
        target = min(latest) if latest else self.state[1]
        matched = {}
        for name in spec['cameras']:
            history = self.image_history[name]
            method = self.sync['image_selection']
            choices = list(history) if method == 'nearest' else [s for s in history if s[1] <= target]
            if method == 'exact': choices = [s for s in choices if s[1] == target]
            if not choices: raise ValueError('No causal camera observation: '+name)
            matched[name] = min(choices,key=lambda s:abs(s[1]-target))
        image_stamps = [sample[1] for sample in matched.values()]
        choices = [s for s in self.state_history if s[1] <= target]
        if not choices: raise ValueError('No causal follower observation')
        state = max(choices,key=lambda s:s[1])
        stamps = [state[1]]
        result = {"observation.state": state[2].copy(), "task": self.spec["task"]}
        if now-state[0] > spec["max_age_s"]:
            raise ValueError("Matched follower observation stale")
        for name in spec["cameras"]:
            sample = matched[name]
            if now-sample[0] > spec["max_age_s"]:
                raise ValueError("Matched camera observation stale: "+name)
            stamps.append(sample[1])
            result[f"observation.images.{name}"] = self.pre(name, image_from_ros(sample[2]))
        if any(abs(s-target)>self.sync['reject_s']+1e-7 for s in stamps):
            raise ValueError("Observation synchronization tolerance exceeded")
        if image_stamps and max(image_stamps)-min(image_stamps)>self.sync['camera_skew_s']+1e-7:
            raise ValueError('Camera-camera synchronization tolerance exceeded')
        if any(not -0.05 <= clock_now-s <= spec["max_age_s"] for s in stamps):
            raise ValueError("Observation header age is invalid")
        return result

    def predict(self, observation, reset):
        if reset:
            self.policy.reset()
            self.scheduler.reset()
        return self.policy.predict(observation)[0] if self.engine is not None else self.scheduler.next(self.policy, observation)

    def tick(self):
        now = time.monotonic()
        if self.engine is not None and self.mode == 'AUTONOMOUS':
            if self.engine_failed:return
            try:
                if self.future is not None:
                    if not self.future.done():return
                    self.future.result();self.future=None
                obs = self.observation()
                if self.reset_pending:
                    self.engine.reset();self.reset_pending=False
                action = self.engine.next(obs)
                self.pub.publish(trajectory(self,self.joints,action))
                self.diagnostic(DiagnosticStatus.OK,'Asynchronous flow action',
                    scheduler=self.engine.method,correction=self.spec['action_chunk']['correction']['method'],
                    inference_latency_s=self.engine.latency_s,missed_inference_deadlines=self.engine.missed,
                    reserved_delay_steps=self.engine.job['delay'] if self.engine.job else None,
                    committed_steps_remaining=max(0, len(self.engine.chunk)-(self.engine.last_step-self.engine.start))
                        if self.engine.chunk is not None else 0,
                    action_smoothing=self.engine.smoothing,
                    raw_chunk_roughness_rad=self.engine.raw_roughness_rad,
                    filtered_chunk_roughness_rad=self.engine.filtered_roughness_rad,
                    smoothing_scope='corrected_commands' if self.engine.correction else 'predicted_plan',
                    correction_latency_ms=self.engine.correction.latency_ms if self.engine.correction else 0.)
            except Exception as error:
                self.engine_failed=True
                self.ready=False;self.ready_pub.publish(Bool(data=False));self.reset_pending=True
                self.diagnostic(DiagnosticStatus.ERROR,'Policy: '+str(error))
            return
        if self.future is not None:
            if not self.future.done():
                if not self.job_warmup and now-self.started > 1/self.spec["inference_rate_hz"]:
                    self.diagnostic(DiagnosticStatus.WARN, "Policy deadline miss", latency_s=now-self.started)
                return
            future, self.future = self.future, None
            try:
                action = future.result()
                # Never publish stale results after mode changes or a long inference.
                self.observation()
                if self.job_warmup:
                    # Warm-up never sends a command or retains an old action chunk.
                    self.scheduler.reset()
                    self.reset_pending = True
                    if self.mode == "IDLE" and self.job_epoch == self.epoch:
                        self.ready = True
                        self.ready_pub.publish(Bool(data=True))
                        self.diagnostic(DiagnosticStatus.OK, "Policy ready (IDLE warm-up complete)")
                elif self.mode == "AUTONOMOUS" and self.job_epoch == self.epoch and now-self.started <= self.config["safety"]["command_timeout_s"]:
                    self.pub.publish(trajectory(self, self.joints, action))
                    self.diagnostic(DiagnosticStatus.OK, "Policy action", latency_s=now-self.started,
                                    target_hz=self.spec["inference_rate_hz"],
                                    action_smoothing=self.scheduler.smoothing,
                                    raw_chunk_roughness_rad=self.scheduler.smoother.raw_roughness_rad,
                                    filtered_chunk_roughness_rad=self.scheduler.smoother.filtered_roughness_rad)
            except Exception as error:
                self.ready = False
                self.ready_pub.publish(Bool(data=False))
                self.reset_pending = True
                self.diagnostic(DiagnosticStatus.ERROR, "Policy: "+str(error))
        if self.mode not in ("IDLE", "AUTONOMOUS") or (self.mode == "IDLE" and self.ready):
            return
        try:
            obs = self.observation()
            self.started, self.job_epoch = now, self.epoch
            self.job_warmup = self.mode == "IDLE"
            reset, self.reset_pending = self.reset_pending or self.job_warmup, False
            self.future = self.worker.submit(self.predict, obs, reset)
        except ValueError as error:
            self.diagnostic(DiagnosticStatus.STALE, str(error))

    def destroy_node(self):
        if self.engine is not None:self.engine.close()
        self.worker.shutdown(wait=True, cancel_futures=True)
        super().destroy_node()


def main():
    run(PolicyNode)

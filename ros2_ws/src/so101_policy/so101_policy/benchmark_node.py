"""Keep benchmark weights resident; only the selected policy may infer or publish."""
import json
import time

from diagnostic_msgs.msg import DiagnosticStatus
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger
from so101_msgs.srv import SelectPolicy
from so101_core.async_chunks import AsyncChunkScheduler
from so101_core.config import load_system, resolve, config_hash
from so101_core.converter import sha256_file
from so101_core.policy import ChunkScheduler
from so101_core.ros_utils import LATCHED_QOS, run
from .node import PolicyNode


class BenchmarkPolicyNode(PolicyNode):
    def __init__(self):
        super().__init__()
        self.declare_parameter('benchmark_models', '[]')
        names = json.loads(self.get_parameter('benchmark_models').value)
        if (not isinstance(names, list) or len(names) != 2 or len(set(names)) != 2
                or self.model_id not in names):
            raise ValueError('Benchmark requires two distinct registered models including model_id')
        self.pool = {self.model_id: (self.policy, self.spec)}
        for name in names:
            if name != self.model_id:
                self.pool[name] = self.load_slot(name)
        if {p.config['objective'] for p, _ in self.pool.values()} != {'standard', 'vlash'}:
            raise ValueError('Benchmark requires standard and VLASH checkpoints')
        self.generation = 0
        self.condition_pub = self.create_publisher(String, '/policy/condition', LATCHED_QOS)
        self.create_service(SelectPolicy, '/policy/select', self.select)
        self.create_service(Trigger, '/policy/check_ready', self.check_ready)
        self.publish_condition()

    def load_slot(self, name):
        root = resolve(self.spec, self.spec['model_root']).resolve()
        folder = (root/name).resolve()
        if not folder.is_relative_to(root) or folder == root:
            raise ValueError('Model ID must remain under model_root')
        manifest = json.loads((folder/'deployment.json').read_text())
        profile = (folder/manifest['runtime_profiles']['none']).resolve()
        if not profile.is_relative_to(folder):
            raise ValueError('Saved profile leaves model folder')
        config = load_system(profile)
        # Pooling is only valid when cameras, physical control and recording agree.
        for key in ('robot', 'camera', 'observation', 'dataset', 'safety', 'controllers', 'teleop'):
            if config_hash(config[key]) != config_hash(self.config[key]):
                raise ValueError('Pooled model runtime differs: '+key)
        for key, expected in [('observation_hash', config_hash(config['observation'])),
                              ('robot_hash', config_hash(config['robot'])), ('units', 'rad'),
                              ('joint_names', self.joints)]:
            if manifest[key] != expected:
                raise ValueError('Pooled model metadata mismatch: '+key)
        if manifest.get('synchronization') != self.sync:
            raise ValueError('Pooled model synchronization differs')
        for relative, expected in manifest['checkpoint_sha256'].items():
            path = (folder/'checkpoint'/relative).resolve()
            if not path.is_relative_to(folder/'checkpoint') or sha256_file(path) != expected:
                raise ValueError('Pooled checkpoint hash mismatch: '+relative)
        for role, expected in manifest.get('calibration_sha256', {}).items():
            if sha256_file(resolve(config['robot'], config['robot'][role]['calibration'])) != expected:
                raise ValueError('Pooled calibration changed: '+role)
        from so101_core.method_training import load_method_policy
        spec = config['policy']
        if spec['action_chunk']['correction']['method'] != 'none':
            raise ValueError('Benchmark does not use A2C2')
        policy = load_method_policy(folder/'checkpoint', spec['device'])
        if (policy.config['objective'] != spec['action_chunk']['scheduler']['method']
                or policy.config['fps'] != spec['inference_rate_hz']
                or policy.config['chunk_size'] != spec['action_chunk']['chunk_size']):
            raise ValueError('Pooled checkpoint/runtime method, FPS or chunk differs')
        policy.reset()
        return policy, spec

    def publish_condition(self):
        self.condition_pub.publish(String(data=json.dumps({
            'model_id': self.model_id, 'method': self.policy.config['objective'],
            'smoothing': self.engine.smoothing, 'generation': self.generation,
            'ready': self.ready, 'resident_models': list(self.pool)}, ensure_ascii=False)))

    def check_ready(self, request, response):
        """Check the inference inputs now, rather than trust a past warm-up result."""
        now = time.monotonic()
        status = {'model_id': self.model_id, 'generation': self.generation,
                  'smoothing': self.engine.smoothing,
                  'camera_receive_age_s': {name: now-self.images[name][0] if name in self.images else None
                                           for name in self.config['observation']['cameras']},
                  'max_age_s': self.config['observation']['max_age_s']}
        try:
            if self.mode != 'IDLE':
                raise ValueError('Readiness check requires IDLE')
            if not self.ready:
                raise ValueError('IDLE warm-up is not complete')
            self.observation()
            response.success = True
        except ValueError as error:
            status['error'] = str(error)
            if self.mode == 'IDLE' and self.ready:
                self.ready = False
                self.ready_pub.publish(Bool(data=False))
                self.reset_pending = True
                self.publish_condition()
        response.message = json.dumps(status, ensure_ascii=False)
        return response

    def select(self, request, response):
        # This callback and tick run on one ROS executor. Never swap an in-flight policy.
        try:
            if self.mode != 'IDLE':
                raise ValueError('Policy selection requires IDLE')
            if request.model_id not in self.pool or request.smoothing not in ('none', 'three_point'):
                raise ValueError('Unknown resident model or smoothing')
            if self.future is not None and not self.future.done():
                raise ValueError('Inference busy; retry after IDLE warm-up')
            if self.engine is not None and self.engine.future is not None and not self.engine.future.done():
                raise ValueError('Inference busy; retry after pending inference finishes')
            self.ready = False
            self.ready_pub.publish(Bool(data=False))
            self.epoch += 1
            if self.future is not None:
                # An old failure is not allowed to become a new-condition result.
                try:
                    self.future.result()
                except Exception:
                    pass
                self.future = None
            if self.engine is not None:
                self.engine.close()
            self.policy, self.spec = self.pool[request.model_id]
            self.model_id = request.model_id
            chunk = self.spec['action_chunk']
            scheduling = chunk['scheduler']
            self.engine = AsyncChunkScheduler(self.policy, scheduling['method'], self.spec['inference_rate_hz'],
                chunk['n_action_steps'], scheduling['max_delay_steps'], scheduling['max_inference_s'],
                smoothing=request.smoothing)
            self.scheduler = ChunkScheduler(chunk['n_action_steps'], 'standard', 'none',
                                           smoothing=request.smoothing, joint_names=self.joints)
            self.policy.reset()
            self.reset_pending = True
            self.job_warmup = False
            self.engine_failed = False
            self.generation += 1
            self.publish_condition()
            response.success, response.message = True, 'Selected; waiting for command-free IDLE warm-up'
        except Exception as error:
            response.success, response.message = False, str(error)
        response.generation = self.generation
        return response

    def tick(self):
        previous = self.ready
        super().tick()
        if hasattr(self, 'condition_pub') and self.ready != previous:
            self.publish_condition()
            if self.ready:
                self.diagnostic(DiagnosticStatus.OK, 'Benchmark condition ready',
                                model_id=self.model_id, generation=self.generation)


def main():
    run(BenchmarkPolicyNode)

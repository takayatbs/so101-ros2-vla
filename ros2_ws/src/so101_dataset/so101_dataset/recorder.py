from pathlib import Path
import shutil
import json
import gzip
import time
import rosbag2_py
from rclpy.serialization import serialize_message
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from rosidl_runtime_py.utilities import get_message
from std_srvs.srv import Trigger
from so101_msgs.srv import StartRecording
from so101_core.ros_utils import SystemNode, run
from so101_core.video import is_keyframe
from so101_core.timing import source_stamp, timestamp_semantics, recording_manifest


def topic_type(topic):
    if topic in ("/tf", "/tf_static"):
        return "tf2_msgs/msg/TFMessage"
    if topic == "/diagnostics":
        return "diagnostic_msgs/msg/DiagnosticArray"
    if topic == "/control_mode" or topic.endswith('/frame_metadata'):
        return "std_msgs/msg/String"
    if topic == "/hardware/motor_enable":
        return "std_msgs/msg/Bool"
    if topic == "/episode/state":
        return "so101_msgs/msg/EpisodeState"
    if topic.endswith("image_raw"):
        return "sensor_msgs/msg/Image"
    if topic.startswith("/camera/") and topic.endswith("/video"):
        return "foxglove_msgs/msg/CompressedVideo"
    if topic.endswith("joint_states") or topic in ("/hardware/follower/state", "/hardware/follower/command"):
        return "sensor_msgs/msg/JointState"
    return "trajectory_msgs/msg/JointTrajectory"


class RecorderNode(SystemNode):
    def __init__(self):
        super().__init__("data_recorder")
        self.writer = None
        self.index_file = None
        self.message_counts = {}
        self.topic_stats = {}
        self.destination = None
        self.latched = {}
        self.video_preroll = {}
        self.subscriptions_kept = []
        self.types = {}
        topics = list(dict.fromkeys([*self.config["dataset"]["topics"],
            *(f'/camera/{name}/frame_metadata' for name in self.config['camera']['cameras'])]))
        for topic in topics:
            type_name = topic_type(topic)
            self.types[topic] = type_name
            state = type_name in ("sensor_msgs/msg/JointState", "sensor_msgs/msg/Image", "tf2_msgs/msg/TFMessage")
            qos = QoSProfile(depth=30 if type_name == "foxglove_msgs/msg/CompressedVideo" else 10 if state else 100,
                             reliability=ReliabilityPolicy.BEST_EFFORT if state else ReliabilityPolicy.RELIABLE)
            if topic in ("/tf_static", "/control_mode", "/episode/state"):
                qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
            self.subscriptions_kept.append(self.create_subscription(
                get_message(type_name), topic, lambda msg, t=topic: self.record(t, msg), qos))
        self.create_service(StartRecording, "/data_recorder/start", self.start)
        self.create_service(Trigger, "/data_recorder/stop", self.stop)
        self.create_timer(2.0, self.check_disk)

    def check_disk(self):
        if self.writer and self.destination:
            if shutil.disk_usage(self.destination).free < self.config["safety"]["min_disk_free_gb"]*1e9:
                self.close_recording()
                self.diagnostic(2, "Recording stopped: insufficient free disk space")

    def start(self, request, response):
        if self.writer is not None:
            response.success, response.message = False, 'Recording already active'
            return response
        try:
            target = Path(request.bag_uri).resolve()
            root = Path(self.config["dataset"]["_root"])/self.config["dataset"]["bag_root"]
            if not target.is_relative_to(root.resolve()):
                raise ValueError("Bag must be under configured bag_root")
            if target.exists():
                raise ValueError("Raw bags are immutable; destination already exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(target.parent).free < self.config["safety"]["min_disk_free_gb"]*1e9:
                raise ValueError("Insufficient disk space")
            writer = rosbag2_py.SequentialWriter()
            writer.open(rosbag2_py.StorageOptions(uri=str(target), storage_id="mcap"),
                        rosbag2_py.ConverterOptions("cdr", "cdr"))
            for index, (topic, type_name) in enumerate(self.types.items()):
                writer.create_topic(rosbag2_py.TopicMetadata(id=index, name=topic, type=type_name,
                                                            serialization_format="cdr"))
            self.writer = writer
            self.destination = target
            self.topic_stats = {}
            self.index_file = gzip.open(target/'message_index.jsonl.gz','xt',compresslevel=1,encoding='utf-8')
            (target.parent/'recording.json').write_text(json.dumps(recording_manifest(self),indent=2))
            for topic, cached in self.latched.items():
                self.write_message(topic,*cached)
            # Keep the original acquisition timestamps. These earlier packets
            # decode the first in-episode delta frames without a second encoder.
            for topic, messages in self.video_preroll.items():
                for cached in messages:
                    self.write_message(topic,*cached)
            response.success, response.message = True, str(target)
        except Exception as error:
            self.close_recording()
            response.success, response.message = False, str(error)
        return response

    def record(self, topic, msg):
        received = self.get_clock().now().nanoseconds
        steady = time.monotonic_ns()
        identity = self.message_counts.get(topic,0)
        self.message_counts[topic] = identity+1
        cached_message = (msg,received,steady,identity)
        if topic in ("/tf_static", "/control_mode", "/episode/state"):
            self.latched[topic] = cached_message
        if self.types[topic] == "foxglove_msgs/msg/CompressedVideo":
            if is_keyframe(msg.data):
                self.video_preroll[topic] = []
            cached = self.video_preroll.get(topic)
            if cached is not None:
                cached.append(cached_message)
                if len(cached) > 120:
                    # Broken/misconfigured GOP: bound memory and await an IDR.
                    self.video_preroll.pop(topic)
        if self.writer is not None:
            try:
                self.write_message(topic,*cached_message)
            except Exception as error:
                self.diagnostic(2, "Recording failed: "+str(error))
                self.close_recording()

    def write_message(self, topic, msg, received, steady, identity):
        source = source_stamp(msg)
        semantics = timestamp_semantics(topic)
        if topic.endswith('/frame_metadata'):
            info = json.loads(msg.data)
            source = info['timestamp_ns']
            semantics = info['timestamp_source']
        serialized = serialize_message(msg)
        write_call = self.get_clock().now().nanoseconds
        self.writer.write(topic,serialized,received)
        item = {'episode_id':self.destination.parent.name,'topic_name':topic,'message_id':identity,
                'frame_id':getattr(msg,'frame_id',getattr(getattr(msg,'header',None),'frame_id','')),
                'source_timestamp_ns':source,'timestamp_semantics':semantics,
                'receive_timestamp_ns':received,'receive_monotonic_ns':steady,
                'write_call_timestamp_ns':write_call}
        if self.index_file is not None:
            self.index_file.write(json.dumps(item,separators=(',',':'))+'\n')
        stats = self.topic_stats.setdefault(topic,{'messages':0,'first_source_ns':source,'last_source_ns':None,
            'timestamp_reversals':0,'duplicate_timestamps':0,'max_source_interval_ns':0,
            'receive_clock_jump_count':0,'last_receive_ns':None,'last_receive_monotonic_ns':None})
        stats['messages'] += 1
        if source is not None and stats['last_source_ns'] is not None:
            delta = source-stats['last_source_ns']
            stats['timestamp_reversals'] += int(delta<0)
            stats['duplicate_timestamps'] += int(delta==0)
            stats['max_source_interval_ns'] = max(stats['max_source_interval_ns'],delta)
        if stats['last_receive_ns'] is not None:
            jump = (received-stats['last_receive_ns'])-(steady-stats['last_receive_monotonic_ns'])
            stats['receive_clock_jump_count'] += int(abs(jump)>10_000_000)
        stats.update(last_source_ns=source,last_receive_ns=received,last_receive_monotonic_ns=steady)

    def close_recording(self):
        active = self.writer is not None or self.index_file is not None
        self.writer = None
        if self.index_file is not None:
            self.index_file.close()
            self.index_file = None
        if active and self.destination is not None:
            for stats in self.topic_stats.values():
                first,last = stats['first_source_ns'],stats['last_source_ns']
                stats['observed_hz'] = (stats['messages']-1)*1e9/(last-first) if first is not None and last is not None and last>first else None
            (self.destination.parent/'recording_summary.json').write_text(json.dumps(self.topic_stats,indent=2))

    def stop(self, request, response):
        if self.writer is None:
            response.success, response.message = False, "No active recording"
        else:
            # Jazzy SequentialWriter closes and flushes on destruction.
            self.close_recording()
            response.success, response.message = True, "MCAP closed"
        return response

    def destroy_node(self):
        self.close_recording()
        super().destroy_node()


def main():
    run(RecorderNode)

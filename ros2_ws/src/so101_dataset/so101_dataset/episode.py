import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from rclpy.callback_groups import ReentrantCallbackGroup
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
from std_msgs.msg import String
from std_srvs.srv import Trigger
from so101_msgs.msg import EpisodeState
from so101_msgs.srv import StartEpisode, StopEpisode, StartRecording
from so101_core.config import resolve, config_hash, calibration_hashes
from so101_core.ros_utils import SystemNode, LATCHED_QOS, run
from so101_core.timing import stamp_ns


class EpisodeNode(SystemNode):
    def __init__(self):
        super().__init__("episode_manager")
        self.active, self.pending = None, False
        self.recorder_error = None
        self.calibration_sha256 = {}
        self.mode = "IDLE"
        self.pub = self.create_publisher(EpisodeState, "/episode/state", LATCHED_QOS)
        group = ReentrantCallbackGroup()
        self.start_client = self.create_client(StartRecording, "/data_recorder/start", callback_group=group)
        self.stop_client = self.create_client(Trigger, "/data_recorder/stop", callback_group=group)
        self.create_subscription(String, "/control_mode", lambda msg: setattr(self, "mode", msg.data), LATCHED_QOS)
        self.create_subscription(DiagnosticArray, "/diagnostics", self.diagnostics, 10)
        self.create_service(StartEpisode, "/start_episode", self.start)
        self.create_service(StopEpisode, "/stop_episode", self.stop)

    def diagnostics(self, msg):
        for status in msg.status:
            if self.active and status.name == "data_recorder" and status.level >= DiagnosticStatus.ERROR:
                self.recorder_error = status.message

    async def start(self, request, response):
        if self.active or self.pending:
            response.message = "An episode is already active or starting"
            return response
        if self.mode not in ("TELEOP", "AUTONOMOUS", "MOVEIT"):
            response.message = "Select an active control mode before recording"
            return response
        if not request.task_id.strip() or not request.operator_name.strip():
            response.message = "task_id and operator are required"
            return response
        if not self.start_client.service_is_ready():
            response.message = "Recorder service is unavailable"
            return response
        self.pending = True
        try:
            self.calibration_sha256 = calibration_hashes(self.config["robot"])
            episode_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:8]
            root = resolve(self.config["dataset"], self.config["dataset"]["bag_root"])
            folder = root/episode_id
            folder.mkdir(parents=True, exist_ok=False)
            result = await self.start_client.call_async(StartRecording.Request(bag_uri=str(folder/"raw")))
            if not result.success:
                response.message = result.message
                return response
            self.recorder_error = None
            self.recording_mode = self.mode
            msg = EpisodeState(episode_id=episode_id, task_id=request.task_id, operator_name=request.operator_name,
                               state="RECORDING", bag_uri=str(folder/"raw"))
            msg.start_time = self.get_clock().now().to_msg()
            self.active = msg
            self.pub.publish(msg)
            metadata = self.metadata(msg)
            metadata["status"] = "incomplete"
            (folder/"episode.json").write_text(json.dumps(metadata, indent=2))
            response.success, response.episode_id, response.message = True, episode_id, str(folder)
        except Exception as error:
            response.message = str(error)
        finally:
            self.pending = False
        return response

    def metadata(self, msg):
        return {"episode_id": msg.episode_id, "task_id": msg.task_id, "operator": msg.operator_name,
                "start_time": msg.start_time.sec+msg.start_time.nanosec/1e9,
                "end_time": msg.end_time.sec+msg.end_time.nanosec/1e9,
                "start_time_ns": stamp_ns(msg.start_time), "end_time_ns": stamp_ns(msg.end_time),
                "recording_manifest": "recording.json", "message_index": "raw/message_index.jsonl.gz",
                "control_mode_at_start": getattr(self,'recording_mode',None),
                "success": msg.success, "failure_reason": msg.failure_reason,
                "bag_uri": msg.bag_uri,
                "observation_hash": config_hash(self.config["observation"]),
                "robot_hash": config_hash(self.config["robot"]),
                "calibration_sha256": self.calibration_sha256}

    async def stop(self, request, response):
        if not self.active or self.pending:
            response.message = "No active episode or an operation is pending"
            return response
        if not self.stop_client.service_is_ready():
            response.message = "Recorder unavailable; episode remains incomplete"
            return response
        self.pending = True
        try:
            msg = self.active
            msg.end_time = self.get_clock().now().to_msg()
            msg.success = request.success and self.recorder_error is None and self.mode not in ("FAULT", "ESTOP")
            msg.failure_reason = self.recorder_error or request.failure_reason
            if calibration_hashes(self.config["robot"]) != self.calibration_sha256:
                msg.success = False
                msg.failure_reason = "Calibration files changed during recording"
            if self.mode in ("FAULT", "ESTOP"):
                msg.failure_reason = msg.failure_reason or "Robot stopped in "+self.mode
            msg.state = "STOPPED"
            self.pub.publish(msg)
            result = await self.stop_client.call_async(Trigger.Request())
            if not result.success:
                response.message = result.message
                return response
            metadata = self.metadata(msg)
            metadata["status"] = "complete"
            (Path(msg.bag_uri).parent/"episode.json").write_text(json.dumps(metadata, indent=2))
            self.active = None
            response.success, response.message = True, msg.episode_id
        finally:
            self.pending = False
        return response


def main():
    run(EpisodeNode)

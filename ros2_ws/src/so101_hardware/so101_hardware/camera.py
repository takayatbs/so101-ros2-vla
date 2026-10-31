import threading
import time
import json
import cv2
import numpy as np
from sensor_msgs.msg import Image, CompressedImage
from diagnostic_msgs.msg import DiagnosticStatus
from std_msgs.msg import String
from rclpy.qos import QoSProfile, ReliabilityPolicy
from so101_core.ros_utils import SystemNode, STATE_QOS, run
from so101_core.timing import stamp_ns


class CameraNode(SystemNode):
    def __init__(self):
        super().__init__("camera_capture")
        self.declare_parameter("camera", "top")
        self.declare_parameter("backend", "mock")
        self.name = self.get_parameter("camera").value
        self.backend = self.get_parameter("backend").value
        self.spec = self.config["camera"]["cameras"][self.name]
        self.preview = self.config["camera"]["preview"]
        self.frame_meta_pub = self.create_publisher(String, f'/camera/{self.name}/frame_metadata',30)
        self.mock_sequence = 0
        self.video = None
        video = self.config["camera"].get("video", {})
        if video.get("enabled", False):
            from foxglove_msgs.msg import CompressedVideo
            from so101_core.video import H264Encoder, AsyncH264Encoder
            self.video_pub = self.create_publisher(CompressedVideo, f"/camera/{self.name}/video",
                QoSProfile(depth=30, reliability=ReliabilityPolicy.RELIABLE))
            def publish_video(data, stamp):
                self.video_pub.publish(CompressedVideo(timestamp=stamp, frame_id=self.spec["frame_id"],
                                                       format="h264", data=data))
            encoder = H264Encoder(self.spec["width"], self.spec["height"], self.spec["fps"],
                                  video.get("qp", 18), video.get("gop", 15))
            self.video = AsyncH264Encoder(encoder, publish_video, video.get("max_age_s", .2))
        self.pub = self.create_publisher(Image, f"/camera/{self.name}/image_raw", STATE_QOS)
        self.preview_pub = self.create_publisher(CompressedImage, f"/camera/{self.name}/preview/compressed", STATE_QOS)
        self.latest, self.last_sequence = None, -1
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.capture, self.thread = None, None
        self.capture_guard, self.capture_error = None, None
        self.capture_started = time.monotonic()
        self.frames, self.window_start, self.dropped = 0, time.monotonic(), 0
        if self.backend == "real":
            self.capture = cv2.VideoCapture(self.spec["device"], cv2.CAP_V4L2)
            if self.spec.get("fourcc") is not None:
                self.capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.spec["fourcc"]))
            for key, prop in [("width", cv2.CAP_PROP_FRAME_WIDTH), ("height", cv2.CAP_PROP_FRAME_HEIGHT),
                               ("fps", cv2.CAP_PROP_FPS)]:
                self.capture.set(prop, self.spec[key])
            # Keep a second V4L2 buffer available while the current frame is
            # decoded; one buffer halves capture FPS on these UVC cameras.
            self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 2)
            if not self.capture.isOpened():
                self.capture.release()
                raise RuntimeError("Camera unavailable: "+self.spec["device"])
            warmup_deadline = time.monotonic()+self.spec.get("warmup_s", 1.)
            while time.monotonic() < warmup_deadline:
                ok, _ = self.capture.read()
                if not ok:
                    self.capture.release()
                    raise RuntimeError("Camera warmup failed: "+self.spec["device"])
            # USB delivery and a separate nominal-FPS timer have different
            # phases. Polling the latest image adds up to one frame of delay
            # and can skip a frame whenever both arrivals fall between ticks.
            # A guard wakes the ROS executor immediately; the latest-only slot
            # still bounds work when processing cannot keep up.
            self.capture_guard = self.create_guard_condition(self.tick)
            self.capture_started = time.monotonic()
            self.capture_watchdog = self.create_timer(
                min(.1, self.config['camera']['timeout_s']/2), self.check_capture)
            self.thread = threading.Thread(target=self.capture_loop, name='capture_'+self.name, daemon=True)
            self.thread.start()
        else:
            self.mock = np.zeros((self.spec["height"], self.spec["width"], 3), dtype=np.uint8)
            self.mock[:] = {"top": (80, 120, 170), "arm": (170, 120, 80), "side": (120, 170, 80)}[self.name]
            cv2.putText(self.mock, "SYNTHETIC "+self.name, (40, 100), cv2.FONT_HERSHEY_SIMPLEX,
                        2, (255, 255, 255), 3)
            self.create_timer(1/self.spec["fps"], self.tick)

    def capture_loop(self):
        sequence = 0
        try:
            while not self.stop_event.is_set():
                if not self.capture.grab():
                    self.stop_event.wait(0.01)
                    continue
                # Preserve acquisition time before MJPEG/RGB conversion.
                # Hardware exposure timestamps are still unavailable.
                stamp, acquired = self.get_clock().now().to_msg(), time.monotonic()
                ok, bgr = self.capture.retrieve()
                if not ok:
                    continue
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                ready_ns = self.get_clock().now().nanoseconds
                sequence += 1
                with self.lock:
                    self.latest = sequence, stamp, acquired, rgb, ready_ns
                if not self.stop_event.is_set() and self.context.ok():
                    self.capture_guard.trigger()
        except Exception as error:
            if not self.stop_event.is_set() and self.context.ok():
                self.capture_error = str(error)

    def check_capture(self):
        if self.capture_error:
            self.diagnostic(DiagnosticStatus.ERROR, 'Camera capture failed: '+self.capture_error)
            return
        with self.lock:
            acquired = self.latest[2] if self.latest is not None else self.capture_started
        if time.monotonic()-acquired > self.config['camera']['timeout_s']:
            self.diagnostic(DiagnosticStatus.STALE, 'Camera frame timeout')

    def tick(self):
        if self.backend == "real":
            with self.lock:
                sample = self.latest
            if sample is None or time.monotonic()-sample[2] > self.config["camera"]["timeout_s"]:
                self.diagnostic(DiagnosticStatus.STALE, "Camera frame timeout")
                return
            sequence, stamp, acquired, rgb, ready_ns = sample
            if sequence == self.last_sequence:
                return
            self.dropped += max(0, sequence-self.last_sequence-1) if self.last_sequence >= 0 else 0
            self.last_sequence = sequence
        else:
            rgb, stamp, acquired = self.mock, self.get_clock().now().to_msg(), time.monotonic()
            self.mock_sequence += 1
            sequence = self.mock_sequence
            ready_ns = stamp_ns(stamp)
        publish_started_ns = self.get_clock().now().nanoseconds
        self.frame_meta_pub.publish(String(data=json.dumps({
            'timestamp_ns':stamp_ns(stamp),'camera':self.name,'frame_id':self.spec['frame_id'],
            'source_sequence':sequence,'sequence_scope':'successfully decoded host frames',
            'width':rgb.shape[1],'height':rgb.shape[0],'configured_fps':self.spec['fps'],
            'timestamp_source':'host_after_grab_before_decode' if self.backend == 'real' else 'synthetic_ROS_clock',
            'hardware_timestamp_ns':None,'hardware_trigger':False,
            'capture_ready_timestamp_ns':ready_ns,
            'publish_started_timestamp_ns':publish_started_ns,
            'metadata_generated_timestamp_ns':self.get_clock().now().nanoseconds})))
        if self.video is not None:
            if self.video.error:
                self.diagnostic(DiagnosticStatus.ERROR, "Video encoding failed: "+self.video.error)
            self.video.submit(rgb, stamp, acquired)
        # The inference branch precedes video compression. No decode round trip.
        if self.pub.get_subscription_count() or self.video is None:
            msg = Image(height=rgb.shape[0], width=rgb.shape[1], encoding="rgb8",
                        is_bigendian=False, step=rgb.shape[1]*3, data=rgb.tobytes())
            msg.header.stamp, msg.header.frame_id = stamp, self.spec["frame_id"]
            self.pub.publish(msg)
        # Encode only when a preview subscriber exists; raw recording is independent.
        if self.video is None and self.preview_pub.get_subscription_count():
            small = cv2.resize(rgb, (self.preview["width"], self.preview["height"]))
            ok, jpeg = cv2.imencode(".jpg", small[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, self.preview["jpeg_quality"]])
            if ok:
                preview = CompressedImage(format="jpeg", data=jpeg.tobytes())
                preview.header = msg.header
                self.preview_pub.publish(preview)
        self.frames += 1
        elapsed = time.monotonic()-self.window_start
        if elapsed >= 2:
            fps = self.frames/elapsed
            video_status = {} if self.video is None else {
                "video_codec": "h264_nvenc", "video_encoded_frames": self.video.frames,
                "video_dropped_before_encode": self.video.dropped, "video_encode_ms": self.video.encode_ms,
                "video_age_ms": self.video.age_ms, "video_error": self.video.error or ""}
            level = DiagnosticStatus.OK if fps >= 0.8*self.spec["fps"] else DiagnosticStatus.WARN
            if self.video is not None and self.video.error:
                level = DiagnosticStatus.ERROR
            self.diagnostic(level,
                            "Camera capture", actual_fps=fps, dropped_frames=self.dropped,
                            frame_age_s=time.monotonic()-acquired, width=rgb.shape[1], height=rgb.shape[0],
                            capture_decode_ms=(ready_ns-stamp_ns(stamp))/1e6,
                            publish_wait_ms=(publish_started_ns-ready_ns)/1e6,
                            **video_status)
            self.frames, self.window_start = 0, time.monotonic()

    def destroy_node(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        # Do not release a VideoCapture concurrently with grab/retrieve.
        if self.capture is not None:
            if self.thread is None or not self.thread.is_alive():
                self.capture.release()
            else:
                self.get_logger().error('Camera capture did not stop; device release deferred until process exit')
        if self.video:
            self.video.close()
        super().destroy_node()


def main():
    run(CameraNode)

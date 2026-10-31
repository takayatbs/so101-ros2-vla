"""Timestamped MP4 storage and bounded random-access decoding for training."""
from collections import OrderedDict
from fractions import Fraction
import io
import os
from pathlib import Path

import numpy as np
from .video import H264Encoder, is_keyframe


def copy_stream(output, template):
    """PyAV 15 renamed the template overload; support the distro PyAV too."""
    if hasattr(output, 'add_stream_from_template'):
        return output.add_stream_from_template(template, opaque=True)
    return output.add_stream(template=template)


class VideoArchive:
    def __init__(self, path, fps, qp=18, gop=15):
        import av
        self.av, self.path, self.fps = av, Path(path), fps
        self.qp, self.gop = qp, gop
        self.output = self.template = self.stream = self.encoder = self.decoder = None
        self.timestamps, self.message_ids = [], []
        self.mode, self.shape, self.frame_id = None, None, ""

    def add(self, value, timestamp_ns, message_id, frame_id="", rgb=False):
        mode = "h264_nvenc" if rgb else "streamcopy"
        if self.mode is not None and self.mode != mode:
            raise ValueError("Camera format changed within episode")
        self.mode = mode
        if self.timestamps and timestamp_ns <= self.timestamps[-1]:
            raise ValueError("Non-monotonic video timestamps")
        if rgb:
            if self.encoder is None:
                self.encoder = H264Encoder(value.shape[1], value.shape[0], self.fps, self.qp, self.gop)
            payload = self.encoder.encode(value)
        else:
            payload = bytes(value)
        if self.output is None:
            if not is_keyframe(payload):
                return None  # Existing bags may start mid-GOP; never invent a reference.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.template = self.av.open(io.BytesIO(payload), format="h264")
            self.output = self.av.open(str(self.path), "w", options={'use_editlist':'0'})
            self.stream = copy_stream(self.output, self.template.streams.video[0])
            # FFmpeg packet durations have a signed 32-bit bound. A nanosecond
            # timescale corrupts gaps longer than ~2.15 s. Keep exact sensor ns
            # in the index and use microsecond PTS for the MP4 container.
            self.stream.time_base = Fraction(1,1_000_000)
            self.decoder = self.av.CodecContext.create("h264", "r")
            self.decoder.thread_count = 1
            self.decoder.options = {"err_detect": "explode"}
        decoded = self.decoder.decode(self.av.Packet(payload))
        if len(decoded) != 1 or decoded[0].is_corrupt:
            raise ValueError("Invalid H.264 reference chain or reordered frames")
        shape = (decoded[0].height, decoded[0].width, 3)
        if self.shape is not None and self.shape != shape:
            raise ValueError("Video resolution changed within episode")
        self.shape = shape
        packet = self.av.Packet(payload)
        packet.stream, packet.time_base = self.stream, Fraction(1,1_000_000)
        packet.pts = packet.dts = round(Fraction(timestamp_ns-(self.timestamps[0] if self.timestamps else timestamp_ns),1000))
        if self.timestamps:
            previous_pts = round(Fraction(self.timestamps[-1]-self.timestamps[0],1000))
            if not 0 < packet.pts-previous_pts <= 2_147_483_647:
                raise ValueError('MP4 frame interval is outside the supported microsecond timescale; check source clock jumps')
        packet.is_keyframe = is_keyframe(payload)
        self.output.mux(packet)
        index = len(self.timestamps)
        self.timestamps.append(timestamp_ns)
        self.message_ids.append(message_id)
        self.frame_id = frame_id
        return index

    def close(self):
        if self.output:
            self.output.close()
            self.output = None
        if self.template:
            self.template.close()
            self.template = None
        if self.encoder:
            self.encoder.close()
            self.encoder = None

    def finalize(self):
        self.close()
        if not self.timestamps:
            raise ValueError("Video has no self-contained decodable frames")
        # PyAV 11 exposes Packet.duration as read-only. Packets constructed from
        # ROS bytes initially have duration=0; a final copy-only mux takes the
        # durations provided by the demuxer, including the last frame. Disabling
        # edit lists on the intermediate file keeps that last frame accessible.
        sealed = self.path.with_suffix('.sealed.mp4')
        with self.av.open(str(self.path)) as source:
            stream = source.streams.video[0]
            with self.av.open(str(sealed),'w',options={'use_editlist':'0','movflags':'faststart'}) as output:
                target = copy_stream(output, stream)
                target.time_base = stream.time_base
                for packet in source.demux(stream):
                    if packet.dts is None: continue
                    packet.stream = target
                    output.mux(packet)
        sealed.replace(self.path)
        timestamps = np.asarray(self.timestamps, dtype=np.int64)
        pts = np.asarray([round(Fraction(int(t-timestamps[0]),1000))*1000 for t in timestamps],dtype=np.int64)
        np.savez_compressed(self.path.with_suffix('.npz'), timestamp_ns=timestamps,
                            pts_ns=pts, message_id=np.asarray(self.message_ids,dtype=np.int64))
        return {"path": self.path.name, "index_path": self.path.with_suffix('.npz').name,
                "frames": len(timestamps), "source_shape": list(self.shape),
                "frame_id": self.frame_id, "codec": "h264", "operation": self.mode,
                "timestamp_origin_ns": int(timestamps[0]), "time_base": [1,1_000_000],
                "maximum_pts_rounding_ns":500, "source_timestamps_exact":True}


class VideoReader:
    """Per-process LRU: at most four open files and 64 MiB of decoded frames."""
    def __init__(self, max_open=4, cache_bytes=64*1024*1024):
        self.pid, self.max_open, self.cache_bytes = os.getpid(), max_open, cache_bytes
        self.opened, self.cache = OrderedDict(), OrderedDict()
        self.used = 0

    def frame(self, path, pts_ns):
        import av
        if self.pid != os.getpid():
            self.close()
            self.pid = os.getpid()
        key = (str(path), int(pts_ns))
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key].copy()
        name = str(path)
        if name not in self.opened:
            if len(self.opened) >= self.max_open:
                _, old = self.opened.popitem(last=False)
                old.close()
            self.opened[name] = av.open(name)
            self.opened[name].streams.video[0].codec_context.thread_count = 1
        self.opened.move_to_end(name)
        container = self.opened[name]
        stream = container.streams.video[0]
        target_pts = round(Fraction(int(pts_ns),1_000_000_000)/stream.time_base)
        container.seek(target_pts, stream=stream, backward=True, any_frame=False)
        for frame in container.decode(stream):
            actual_ns = round(frame.pts*frame.time_base*1_000_000_000)
            if actual_ns == pts_ns:
                if frame.is_corrupt:
                    raise ValueError("Corrupt video frame")
                rgb = frame.to_ndarray(format="rgb24")
                while self.cache and self.used+rgb.nbytes > self.cache_bytes:
                    _, old = self.cache.popitem(last=False)
                    self.used -= old.nbytes
                if rgb.nbytes <= self.cache_bytes:
                    self.cache[key] = rgb
                    self.used += rgb.nbytes
                return rgb.copy()
            if actual_ns > pts_ns:
                break
        raise ValueError(f"Video frame timestamp not found: {path}, pts_ns={pts_ns}")

    def close(self):
        for container in self.opened.values(): container.close()
        self.opened.clear()
        self.cache.clear()
        self.used = 0

    def __del__(self):
        self.close()

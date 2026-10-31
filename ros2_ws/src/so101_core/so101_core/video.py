"""One low-delay H.264 stream per camera, shared by recording and viewers.

PyAV is imported lazily so original RGB profiles remain usable without it.
Each Annex B access unit contains exactly one image; B frames are forbidden.
"""
from fractions import Fraction
import re
import threading
import time

import numpy as np


def nal_types(data):
    return {part[0] & 31 for part in re.split(b"\x00\x00\x00?\x01", bytes(data))[1:] if part}


def is_keyframe(data):
    return {5, 7, 8} <= nal_types(data)


class H264Encoder:
    def __init__(self, width, height, fps, qp=18, gop=15):
        import av
        if min(width, height, fps, gop) <= 0 or width % 2 or height % 2 or not 0 <= qp <= 51:
            raise ValueError("Invalid NVENC dimensions/rate/quality/GOP")
        self.av, self.shape, self.sequence = av, (height, width, 3), 0
        self.codec = av.CodecContext.create("h264_nvenc", "w")
        self.codec.width, self.codec.height, self.codec.pix_fmt = width, height, "yuv420p"
        rate = Fraction(str(fps)).limit_denominator(100000)
        self.codec.time_base, self.codec.framerate = 1/rate, rate
        self.codec.gop_size, self.codec.thread_count = gop, 1
        self.codec.options = {"preset": "p4", "tune": "ull", "rc": "constqp", "qp": str(qp),
                              "rc-lookahead": "0", "zerolatency": "1", "delay": "0", "bf": "0"}
        self.codec.open()  # Fail explicitly; never silently fall back to a CPU encoder.

    def encode(self, rgb):
        if rgb.dtype != np.uint8 or rgb.shape != self.shape:
            raise ValueError("Capture dimensions differ from NVENC configuration")
        frame = self.av.VideoFrame.from_ndarray(rgb, format="rgb24")
        frame.pts = self.sequence
        packets = self.codec.encode(frame)
        if len(packets) != 1 or packets[0].pts != self.sequence:
            raise RuntimeError("NVENC must output one frame immediately, without frame reordering")
        packet = packets[0]
        payload = bytes(packet)
        if packet.is_keyframe and not is_keyframe(payload):
            raise RuntimeError("H.264 IDR is missing SPS/PPS headers")
        self.sequence += 1
        return payload

    def close(self):
        codec, self.codec = self.codec, None
        # PyAV 15 releases CodecContext when its last reference is dropped;
        # older distro builds expose an explicit close() method.
        close = getattr(codec, 'close', None)
        if close is not None:
            close()


class AsyncH264Encoder:
    """Bound work to one waiting image; never queue an ever-older live stream.

    Images may be replaced BEFORE encoding. Encoded reference packets are
    always published in order and never intentionally dropped here.
    """
    def __init__(self, encoder, publish, max_age_s=.2):
        self.encoder, self.publish, self.max_age_s = encoder, publish, max_age_s
        self.condition = threading.Condition()
        self.pending, self.stopped, self.error = None, False, None
        self.dropped, self.frames, self.encode_ms, self.age_ms = 0, 0, 0., 0.
        self.thread = threading.Thread(target=self._run, name="nvenc", daemon=True)
        self.thread.start()

    def submit(self, rgb, stamp, acquired):
        with self.condition:
            if self.stopped or self.error:
                return
            if self.pending is not None:
                self.dropped += 1
            self.pending = rgb, stamp, acquired
            self.condition.notify()

    def _run(self):
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.stopped or self.pending is not None)
                    if self.stopped:
                        return
                    rgb, stamp, acquired = self.pending
                    self.pending = None
                start = time.monotonic()
                if start-acquired > self.max_age_s:
                    self.dropped += 1
                    continue
                data = self.encoder.encode(rgb)
                self.encode_ms = (time.monotonic()-start)*1000
                self.publish(data, stamp)
                self.age_ms = (time.monotonic()-acquired)*1000
                self.frames += 1
        except Exception as error:
            self.error = str(error)
        finally:
            self.encoder.close()

    def close(self):
        with self.condition:
            self.stopped = True
            self.pending = None
            self.condition.notify()
        self.thread.join(timeout=2)


class H264Decoder:
    def __init__(self):
        import av
        self.av = av
        self.codec = av.CodecContext.create("h264", "r")
        self.codec.thread_count = 1
        self.started = False

    def decode(self, data):
        if not self.started:
            if not is_keyframe(data):
                return None
            self.started = True
        frames = self.codec.decode(self.av.Packet(bytes(data)))
        if len(frames) != 1:
            raise ValueError("Expected exactly one decodable H.264 image per message (no B frames)")
        return frames[0].to_ndarray(format="rgb24")

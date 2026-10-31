import threading
import time

import numpy as np
import pytest

from so101_core.video import AsyncH264Encoder, H264Encoder, H264Decoder, is_keyframe


def test_encoder_queue_replaces_waiting_images_without_reordering_packets():
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    class SlowEncoder:
        def encode(self, image):
            if image == 0:
                entered.set()
                assert release.wait(3)
            return bytes([image])
        def close(self): pass
    received = []
    def receive(data, stamp):
        received.append((data, stamp))
        if len(received) == 2: done.set()
    encoder = AsyncH264Encoder(SlowEncoder(), receive, max_age_s=10)
    try:
        encoder.submit(0, 100, time.monotonic())
        assert entered.wait(2)
        for i in range(1, 11): encoder.submit(i, 100+i, time.monotonic())
        release.set()
        assert done.wait(2)
        assert received == [(b'\x00', 100), (b'\x0a', 110)]
        assert encoder.dropped == 9 and encoder.error is None
    finally:
        release.set()
        encoder.close()
    assert not encoder.thread.is_alive()


def test_nvenc_immediate_packets_and_mid_stream_decoder():
    pytest.importorskip('av')
    try:
        encoder = H264Encoder(160, 120, 30, gop=5)
    except Exception as error:
        pytest.skip(f'NVENC not available: {error}')
    decoder = H264Decoder()
    image = np.full((120, 160, 3), [180, 70, 30], dtype=np.uint8)
    packets = []
    try:
        for _ in range(12): packets.append(encoder.encode(image))
    finally:
        encoder.close()
    assert [i for i, p in enumerate(packets) if is_keyframe(p)] == [0, 5, 10]
    # A late viewer skips dependent packets until a self-contained IDR arrives.
    assert decoder.decode(packets[3]) is None
    assert decoder.decode(packets[4]) is None
    for packet in packets[5:]:
        rgb = decoder.decode(packet)
        assert rgb.shape == image.shape
        assert np.abs(rgb.astype(float)-image).mean() < 5

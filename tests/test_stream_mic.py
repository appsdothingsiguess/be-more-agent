import wave

import numpy as np
import pytest

from app.audio.capture import FRAME_SAMPLES
from app.config import Config
from app.hardware.stream_mic import StreamMicrophone


class FakeCapture:
    rate = 16000

    def __init__(self, pre=()):
        self.pre, self.sinks = list(pre), []

    def attach(self, fn):
        self.sinks.append(fn)
        return list(self.pre)

    def remove_sink(self, fn):
        if fn in self.sinks:
            self.sinks.remove(fn)

    def push(self, frame):
        for fn in list(self.sinks):
            fn(frame)


def frame(v=100):
    return np.full(FRAME_SAMPLES, v, dtype=np.int16)


def make(tmp_path, pre=(), file_mic=None):
    cfg = Config()
    cap = FakeCapture(pre)
    return StreamMicrophone(cfg, cap, file_mic=file_mic, runtime_dir=tmp_path), cap


def test_wav_includes_preroll(tmp_path):
    mic, cap = make(tmp_path, pre=[frame(1)] * 2)
    mic.start()
    assert mic.is_recording
    for _ in range(5):
        cap.push(frame(2))
    path = mic.stop()
    assert path == tmp_path / "mic_upload.wav" and not mic.is_recording and not cap.sinks
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        assert w.getnframes() == 7 * FRAME_SAMPLES


def test_too_short_returns_none(tmp_path):
    mic, cap = make(tmp_path)
    mic.start()
    cap.push(frame())  # 0.08 s < min_seconds
    assert mic.stop() is None
    assert not (tmp_path / "mic_upload.wav").exists()


def test_abort_discards(tmp_path):
    mic, cap = make(tmp_path)
    mic.start()
    for _ in range(10):
        cap.push(frame())
    mic.abort()
    assert not mic.is_recording and not cap.sinks
    assert mic.stop() is None


def test_double_start_raises_and_max_seconds_caps(tmp_path):
    mic, cap = make(tmp_path)
    mic.cfg.microphone.max_seconds = 0.4
    mic.start()
    with pytest.raises(RuntimeError):
        mic.start()
    for _ in range(10):
        cap.push(frame())
    path = mic.stop()
    with wave.open(str(path)) as w:
        assert w.getnframes() == 5 * FRAME_SAMPLES


def test_configure_delegates_without_capture(tmp_path):
    calls = []
    fm = type("M", (), {"configure": lambda self: calls.append(1)})()
    mic, _ = make(tmp_path, file_mic=fm)
    mic.configure()
    assert calls == [1]
    make(tmp_path)[0].configure()  # no file mic: no-op

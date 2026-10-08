import io
import threading

import numpy as np

from app.audio.capture import FRAME_SAMPLES, AudioCapture
from app.config import Config
from app.hardware.alsa import Card

CARDS = [Card(1, "Device", "USB PnP Sound Device", "C-Media USB PnP Sound Device")]


class FakeProc:
    def __init__(self, data):
        self.stdout = io.BytesIO(data)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


def pcm(value, frames=1):
    return np.full(FRAME_SAMPLES * frames, value, dtype="<i2").tobytes()


def make(cfg_edit=None, datas=(), **kw):
    cfg = Config()
    cfg.microphone.gain_db = 0.0
    if cfg_edit:
        cfg_edit(cfg)
    cmds, queue = [], list(datas)

    def popen(cmd, **k):
        cmds.append(cmd)
        return FakeProc(queue.pop(0) if queue else b"")
    cap = AudioCapture(cfg, popen=popen, cards=CARDS, backoff=(0.01, 0.02), **kw)
    return cap, cfg, cmds


def wait_for(pred, timeout=2.0):
    ev = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if pred():
            return True
        ev.wait(0.01)
    return False


def test_command_uses_plughw_and_rate():
    cap, _, _ = make()
    cmd = cap.command()
    assert cmd[:3] == ["arecord", "-q", "-t"]
    assert "plughw:CARD=Device,DEV=0" in cmd and "16000" in cmd


def test_gain_and_clipping():
    cap, cfg, _ = make(lambda c: setattr(c.microphone, "gain_db", 20.0))
    got = []
    cap.add_sink(got.append)
    cap._dispatch(np.array([100, 5000, -5000] + [0] * (FRAME_SAMPLES - 3), dtype="<i2"))
    assert list(got[0][:3]) == [1000, 32767, -32768]
    cfg.microphone.gain_db = 0.0  # live change
    cap._dispatch(np.full(FRAME_SAMPLES, 7, dtype="<i2"))
    assert got[1][0] == 7


def test_fanout_preroll_and_remove():
    cap, _, _ = make(lambda c: setattr(c.listen, "preroll_seconds", 0.16))
    a, b = [], []
    cap.add_sink(a.append)
    cap.add_sink(b.append)
    for v in (1, 2, 3):
        cap._dispatch(np.full(FRAME_SAMPLES, v, dtype="<i2"))
    assert len(a) == len(b) == 3
    assert [int(f[0]) for f in cap.preroll()] == [2, 3]  # ring keeps 2 frames
    cap.remove_sink(a.append)  # bound methods compare equal
    cap._dispatch(np.zeros(FRAME_SAMPLES, dtype="<i2"))
    assert len(a) == 3 and len(b) == 4


def test_bad_sink_does_not_break_others():
    cap, _, _ = make()
    got = []

    def bad(_):
        raise RuntimeError("x")
    cap.add_sink(bad)
    cap.add_sink(got.append)
    cap._dispatch(np.zeros(FRAME_SAMPLES, dtype="<i2"))
    assert len(got) == 1


def test_restart_on_eof_publishes_and_recovers():
    events, recovered = [], []
    cap, _, cmds = make(datas=[pcm(1, 2), b"", pcm(2, 1)], publish=events.append)
    cap.on_recover = lambda: recovered.append(1)
    got = []
    cap.add_sink(got.append)
    cap.start()
    assert wait_for(lambda: len(got) >= 3 and recovered)
    cap.stop()
    assert len(cmds) >= 3
    assert events[0] == {"type": "live", "armed": False, "model": None,
                         "error": "Microphone unavailable"}
    assert [int(f[0]) for f in got[:3]] == [1, 1, 2]


def test_stop_terminates_cleanly():
    cap, _, _ = make(datas=[pcm(1, 1)])
    cap.start()
    cap.stop()
    assert cap._thread is None

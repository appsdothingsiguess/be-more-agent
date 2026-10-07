import subprocess
import threading
import time

from app.config import SpeakerConfig
from app.hardware.alsa import Card
from app.hardware.speaker import Speaker

CARDS = [Card(2, "UACDemoV10", "UACDemoV1.0", "Jieli UACDemoV1.0")]


def make(tmp_path):
    argvs, procs = [], []

    def popen(argv, **kw):
        argvs.append(argv)
        p = subprocess.Popen(["sleep", "5"])
        procs.append(p)
        return p

    return Speaker(SpeakerConfig(), tmp_path, popen=popen, cards=CARDS), argvs, procs


def test_stop_unblocks(tmp_path):
    sp, argvs, procs = make(tmp_path)
    res = []
    t = threading.Thread(target=lambda: res.append(sp.play("x.wav")))
    t.start()
    time.sleep(0.3)
    assert sp.is_playing
    t0 = time.time()
    sp.stop()
    t.join(3)
    assert res == [False] and time.time() - t0 < 2
    assert argvs[0] == ["aplay", "-q", "-D", "plughw:CARD=UACDemoV10,DEV=0", "x.wav"]
    sp.stop()  # safe when idle


def test_preempt(tmp_path):
    sp, _, procs = make(tmp_path)
    assert sp.play("a.wav", block=False)
    assert sp.play("b.wav", block=False)
    assert procs[0].poll() is not None
    assert procs[1].poll() is None
    sp.stop()


def test_effects(tmp_path):
    sp, argvs, _ = make(tmp_path)
    assert sp.random_sound("ack") is None and not sp.play_effect("ack")
    d = tmp_path / "ack_sounds"
    d.mkdir()
    (d / "a.wav").write_bytes(b"x")
    assert sp.random_sound("ack") == d / "a.wav"
    assert sp.play_effect("ack")
    sp.stop()

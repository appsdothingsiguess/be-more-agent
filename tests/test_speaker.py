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


def test_configure_sets_default_volume(tmp_path):
    calls = []
    run = lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
    Speaker(SpeakerConfig(), tmp_path, cards=CARDS, run=run).configure()
    assert calls == [["amixer", "-c", "UACDemoV10", "sset", "PCM", "50%", "unmute"]]


def test_configure_tolerates_failure_and_can_be_disabled(tmp_path):
    def boom(argv, **kw):
        raise OSError("no amixer")
    Speaker(SpeakerConfig(), tmp_path, cards=CARDS, run=boom).configure()  # no raise
    calls = []
    Speaker(SpeakerConfig(set_volume=False), tmp_path, cards=CARDS,
            run=lambda a, **k: calls.append(a)).configure()
    assert calls == []


def test_replugged_speaker_is_redetected_and_volume_reapplied(tmp_path):
    cards, argvs, mixer = [], [], []

    def popen(argv, **kw):
        argvs.append(argv)
        return subprocess.Popen(["true"])

    def run(cmd, **kw):
        mixer.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    sp = Speaker(SpeakerConfig(), tmp_path, popen=popen, cards=cards, run=run)
    assert sp.resolved.source == "fallback"  # started while unplugged
    cards.append(Card(3, "UACDemoV10", "UACDemoV1.0", "Jieli UACDemoV1.0"))  # plugged in as card 3
    sp.play("a.wav")
    assert argvs[-1][3] == "plughw:CARD=UACDemoV10,DEV=0"
    assert sp.card == "UACDemoV10" and mixer[-1][:3] == ["amixer", "-c", "UACDemoV10"]
    sp.play("b.wav")
    assert len(mixer) == 1  # unchanged device: no extra mixer calls


def _stream_speaker(tmp_path, cmd):
    argvs = []

    def popen(argv, **kw):
        argvs.append(argv)
        return subprocess.Popen(cmd, **kw)
    return Speaker(SpeakerConfig(), tmp_path, popen=popen, cards=CARDS), argvs


def test_stream_writes_pieces_into_one_aplay(tmp_path):
    out = tmp_path / "out.raw"
    sp, argvs = _stream_speaker(tmp_path, ["sh", "-c", f"cat > {out}"])
    s = sp.open_stream(22050, 1)
    assert s.write(b"ab") and s.write(b"cd")
    assert s.close()
    assert out.read_bytes() == b"abcd" and len(argvs) == 1
    assert argvs[0] == ["aplay", "-q", "-D", "plughw:CARD=UACDemoV10,DEV=0", "-t", "raw",
                        "-f", "S16_LE", "-r", "22050", "-c", "1"]
    assert not sp.is_playing


def test_stop_ends_a_stream(tmp_path):
    sp, _ = _stream_speaker(tmp_path, ["sleep", "5"])
    s = sp.open_stream(22050)
    sp.stop()
    assert not s.write(b"ab")
    t0 = time.time()
    assert not s.close() and time.time() - t0 < 2


def test_stream_abort_spares_a_newer_sound(tmp_path):
    sp, _ = _stream_speaker(tmp_path, ["sleep", "5"])
    s = sp.open_stream(22050)
    assert sp.play("ding.wav", block=False)
    s.abort()
    assert sp.is_playing     # the sound started after the stream keeps playing
    sp.stop()

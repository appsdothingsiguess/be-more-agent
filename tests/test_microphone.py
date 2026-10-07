import signal
import subprocess
from types import SimpleNamespace

from app.config import MicrophoneConfig
from app.hardware.alsa import Card
from app.hardware.microphone import Microphone
from tests.wavutil import make_wav

CARDS = [Card(1, "Device", "USB PnP Sound Device", "C-Media USB PnP Sound Device")]


class FakeProc:
    def __init__(self, argv, wav_secs):
        self.argv, self.signals, self.returncode = argv, [], None
        self.stderr = None
        make_wav(argv[-1], secs=wav_secs)

    def poll(self):
        return self.returncode

    def send_signal(self, s):
        self.signals.append(s)
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def make(tmp_path, wav_secs):
    procs, cmds = [], []

    def popen(argv, **kw):
        p = FakeProc(argv, wav_secs)
        procs.append(p)
        return p

    def fake_run(argv, **kw):
        cmds.append(argv)
        if argv[0] == "amixer":
            return SimpleNamespace(returncode=1, stderr="no such control")
        return subprocess.run(argv, **kw)

    mic = Microphone(MicrophoneConfig(), tmp_path / "rt", run=fake_run,
                     popen=popen, cards=CARDS)
    return mic, procs, cmds


def test_record_flow(tmp_path):
    mic, procs, cmds = make(tmp_path, 0.6)
    mic.configure()  # amixer fails -> tolerated
    assert [c[:4] for c in cmds] == [["amixer", "-c", "Device", "sset"]] * 2
    mic.start()
    assert mic.is_recording
    argv = procs[0].argv
    assert "plughw:CARD=Device,DEV=0" in argv and "48000" in argv and "30" in argv
    out = mic.stop()
    assert procs[0].signals == [signal.SIGINT]
    assert out is not None and out.exists()


def test_short_returns_none(tmp_path):
    mic, _, _ = make(tmp_path, 0.05)
    mic.start()
    assert mic.stop() is None


def test_abort(tmp_path):
    mic, procs, _ = make(tmp_path, 0.6)
    mic.start()
    mic.abort()
    assert procs[0].returncode == -9 and not mic.is_recording

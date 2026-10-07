import pytest

from app.config import Config
from app.diagnostics import run_diagnostics
from app.server import BMOClient, Reservation
from tests.fake_bmo_server import FakeBMOServer
from tests.wavutil import make_wav


class FakeMic:
    def __init__(self, tmp_path):
        self.path = tmp_path / "mic.wav"

    def configure(self):
        pass

    def record(self, seconds):
        make_wav(self.path, rate=16000, secs=0.5)
        return self.path


class FakeSpeaker:
    def __init__(self):
        self.played = []

    def play(self, path, block=True):
        self.played.append(str(path))
        return True


class FakeCamera:
    def __init__(self, tmp_path, fail=False):
        self.path = tmp_path / "cam.jpg"
        self.fail = fail

    def available(self):
        return not self.fail

    def capture(self):
        if self.fail:
            raise RuntimeError("no camera")
        self.path.write_bytes(b"\xff\xd8" + b"x" * 2000)
        return self.path


@pytest.fixture
def server():
    s = FakeBMOServer().start()
    yield s
    s.stop()


def build(server, tmp_path, camera_fail=False):
    cfg = Config(server_url=server.url, connect_timeout=2.0, request_timeout=10.0,
                 runtime_dir=str(tmp_path / "rt"), readiness_poll_seconds=0.01)
    client = BMOClient(cfg, token="tok-secret-123")
    comps = {
        "client": client,
        "reservation": Reservation(client, cfg),
        "mic": FakeMic(tmp_path),
        "speaker": FakeSpeaker(),
        "camera": FakeCamera(tmp_path, fail=camera_fail),
    }
    return cfg, comps


def run(cfg, comps, out):
    return run_diagnostics(cfg, components=comps, skip_interactive=True,
                           out=out.append, record_seconds=0.0, cancel_after=0.2)


def cancels(server):
    return [r for r in server.requests if r["path"].endswith("/cancel")]


def test_all_pass(server, tmp_path):
    cfg, comps = build(server, tmp_path)
    server.interact_delay = 5.0
    lines = []
    assert run(cfg, comps, lines) == 0
    assert any("[13/15] cancellation ... PASS" in ln for ln in lines)
    assert len(cancels(server)) == 1
    assert server.find("DELETE", "/v1/bmo/reservation")
    assert "tok-secret-123" not in "\n".join(lines)
    assert comps["speaker"].played


def test_release_when_step_raises(server, tmp_path, monkeypatch):
    cfg, comps = build(server, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("chat exploded")

    monkeypatch.setattr(comps["client"], "chat", boom)
    lines = []
    assert run(cfg, comps, lines) == 1
    assert any("text chat ... FAIL" in ln for ln in lines)
    assert server.find("DELETE", "/v1/bmo/reservation")


def test_release_on_keyboard_interrupt(server, tmp_path, monkeypatch):
    cfg, comps = build(server, tmp_path)

    def stop(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(comps["client"], "chat", stop)
    assert run(cfg, comps, []) == 1
    assert server.find("DELETE", "/v1/bmo/reservation")


def test_camera_failure_continues(server, tmp_path):
    cfg, comps = build(server, tmp_path, camera_fail=True)
    lines = []
    assert run(cfg, comps, lines) == 1
    assert any("camera ... FAIL" in ln for ln in lines)
    assert any("vision ... SKIP" in ln for ln in lines)
    assert any("[15/15] release ... PASS" in ln for ln in lines)
    assert server.find("DELETE", "/v1/bmo/reservation")

import io
import subprocess
import sys
from pathlib import Path

from app.hardware.input import Action
from app.ui import BotState, HeadlessUI
from app.ui import gui

FACES = Path(__file__).resolve().parent.parent / "faces"


class FakeController:
    def __init__(self):
        self.calls = []

    def handle_action(self, a):
        self.calls.append(("action", a))

    def submit_text(self, t):
        self.calls.append(("text", t))

    def shutdown(self):
        self.calls.append(("shutdown",))


def test_headless_flow():
    ui = HeadlessUI(stdin=io.StringIO("\ni\nt hello\nq\n"), out=lambda s: None)
    c = FakeController()
    ui.run(c)
    assert c.calls == [("action", Action.START), ("action", Action.INTERRUPT),
                       ("text", "hello"), ("action", Action.QUIT), ("shutdown",)]


def test_headless_eof_and_output():
    out = []
    ui = HeadlessUI(stdin=io.StringIO(""), out=out.append)
    c = FakeController()
    ui.run(c)
    assert c.calls == [("action", Action.QUIT), ("shutdown",)]
    ui.set_state(BotState.IDLE, "Ready")
    ui.show_text("hi", "user")
    ui.show_text("yo")
    assert out[-3:] == ["[IDLE] Ready", "You: hi", "BMO: yo"]


def test_gui_import_is_tk_free():
    code = "import sys, app.ui, app.ui.gui; sys.exit('tkinter' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_load_face_frames_real_dir():
    frames = gui.load_face_frames(FACES)
    assert set(frames) == set(BotState)
    assert all(frames[s] for s in BotState)


def test_gui_available_without_display(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert gui.gui_available() is False

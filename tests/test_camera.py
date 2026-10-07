from types import SimpleNamespace

import pytest
from PIL import Image

from app.config import CameraConfig
from app.hardware.camera import Camera, CameraError


def make(tmp_path, tools=("rpicam-still",), size=2000, rotation=0, jpeg=False):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        out = argv[argv.index("-o") + 1]
        if jpeg:
            Image.new("RGB", (40, 20), "red").save(out, format="JPEG", quality=100)
            with open(out, "ab") as f:
                f.write(b"\0" * 2000)  # ensure > 1000 bytes
        else:
            with open(out, "wb") as f:
                f.write(b"x" * size)
        return SimpleNamespace(returncode=0, stderr="")

    cam = Camera(CameraConfig(rotation=rotation), tmp_path,
                 run=run, which=lambda t: t if t in tools else None)
    return cam, calls


def test_capture(tmp_path):
    cam, calls = make(tmp_path)
    out = cam.capture()
    assert out == tmp_path / "camera.jpg"
    argv, kw = calls[0]
    assert argv[:7] == ["rpicam-still", "--width", "640", "--height", "480", "-n", "-t"]
    assert "--rotation" not in argv and kw["timeout"] == 30.0


def test_fallback_tool_and_none(tmp_path):
    cam, calls = make(tmp_path, tools=("libcamera-still",))
    cam.capture()
    assert calls[0][0][0] == "libcamera-still"
    cam, _ = make(tmp_path, tools=())
    assert not cam.available()
    with pytest.raises(CameraError):
        cam.capture()


def test_tiny(tmp_path):
    cam, _ = make(tmp_path, size=10)
    with pytest.raises(CameraError):
        cam.capture()


def test_rotation(tmp_path):
    cam, calls = make(tmp_path, rotation=180)
    cam.capture()
    assert calls[0][0][-2:] == ["--rotation", "180"]
    cam, _ = make(tmp_path, rotation=90, jpeg=True)
    out = cam.capture()
    assert Image.open(out).size == (20, 40)

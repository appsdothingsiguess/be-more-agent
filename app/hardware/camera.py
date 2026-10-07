"""Raspberry Pi camera capture via rpicam-still / libcamera-still."""
import shutil
import subprocess
from pathlib import Path

from app.config import CameraConfig


class CameraError(Exception):
    pass


class Camera:
    TOOLS = ("rpicam-still", "libcamera-still")

    def __init__(self, cfg: CameraConfig, runtime_dir: Path,
                 run=subprocess.run, which=shutil.which):
        self.cfg = cfg
        self.runtime_dir = Path(runtime_dir)
        self._run = run
        self._which = which

    def _tool(self) -> str | None:
        for t in self.TOOLS:
            if self._which(t):
                return t
        return None

    def available(self) -> bool:
        return self._tool() is not None

    def capture(self, path=None) -> Path:
        tool = self._tool()
        if tool is None:
            raise CameraError("No Raspberry Pi camera capture utility found")
        out = Path(path) if path else self.runtime_dir / "camera.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        cmd = [tool, "--width", str(self.cfg.width), "--height", str(self.cfg.height),
               "-n", "-t", "1000", "-o", str(out)]
        if self.cfg.rotation == 180:
            cmd += ["--rotation", "180"]
        try:
            r = self._run(cmd, capture_output=True, text=True,
                          timeout=self.cfg.timeout_seconds)
        except subprocess.TimeoutExpired as e:
            raise CameraError(f"camera capture timed out after {self.cfg.timeout_seconds}s") from e
        except OSError as e:
            raise CameraError(f"camera tool failed to run: {e}") from e
        if r.returncode != 0:
            raise CameraError(f"{tool} failed (rc={r.returncode}): {(r.stderr or '')[-300:]}")
        if not out.exists() or out.stat().st_size <= 1000:
            raise CameraError("camera produced no usable image")
        if self.cfg.rotation in (90, 270):
            from PIL import Image
            with Image.open(out) as img:
                rotated = img.rotate(self.cfg.rotation, expand=True)
            rotated.save(out, format="JPEG")
        return out

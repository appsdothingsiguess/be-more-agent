"""Drop-in Microphone replacement that records from the shared AudioCapture stream."""
from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class StreamMicrophone:
    def __init__(self, cfg, capture, file_mic=None, runtime_dir: Path | None = None):
        self.cfg = cfg
        self.capture = capture
        self.file_mic = file_mic
        self.runtime_dir = Path(runtime_dir) if runtime_dir else cfg.runtime_path
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.upload_path = self.runtime_dir / "mic_upload.wav"
        self._lock = threading.Lock()
        self._frames: list[np.ndarray] | None = None
        self._samples = 0

    def configure(self) -> None:
        """Mixer setup only (never opens the device)."""
        if self.file_mic is not None:
            self.file_mic.configure()

    @property
    def is_recording(self) -> bool:
        return self._frames is not None

    def _on_frame(self, frame: np.ndarray) -> None:
        with self._lock:
            if self._frames is None:
                return
            if self._samples < self.cfg.microphone.max_seconds * self.capture.rate:
                self._frames.append(frame)
                self._samples += len(frame)

    def start(self) -> None:
        with self._lock:
            if self._frames is not None:
                raise RuntimeError("already recording")
            pre = self.capture.attach(self._on_frame)
            self._frames = list(pre)
            self._samples = sum(len(f) for f in pre)
        self.upload_path.unlink(missing_ok=True)

    def _take(self) -> list[np.ndarray] | None:
        self.capture.remove_sink(self._on_frame)
        with self._lock:
            frames, self._frames, self._samples = self._frames, None, 0
        return frames

    def stop(self) -> Path | None:
        frames = self._take()
        if not frames:
            return None
        pcm = np.concatenate(frames).astype("<i2")
        rate = self.capture.rate
        if len(pcm) / rate < self.cfg.microphone.min_seconds:
            return None
        with wave.open(str(self.upload_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.tobytes())
        return self.upload_path

    def abort(self) -> None:
        self._take()

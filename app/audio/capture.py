"""One long-lived arecord process feeding fixed-size frames to cheap sinks.

Only one process can open the USB mic, so wake word, VAD and the recorded clip
all share this stream. Frames are int16 numpy arrays of FRAME_SAMPLES (80 ms at
16 kHz) with the live ``microphone.gain_db`` already applied.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from collections import deque
from typing import Callable

import numpy as np

from app.hardware import alsa

log = logging.getLogger(__name__)

FRAME_SAMPLES = 1280
BACKOFF = (1.0, 30.0)  # first restart delay, cap (seconds)


class AudioCapture:
    def __init__(self, cfg, publish: Callable[[dict], None] | None = None,
                 popen=subprocess.Popen, cards=None, backoff: tuple[float, float] = BACKOFF):
        self.cfg = cfg
        self.publish = publish
        self._popen = popen
        self._backoff = backoff
        mic = cfg.microphone
        self.rate = int(mic.stream_rate)
        self.frame_s = FRAME_SAMPLES / self.rate
        self.device = alsa.resolve_device(mic.device, mic.match, mic.fallback_device, cards).device
        n = max(1, int(round(cfg.listen.preroll_seconds / self.frame_s)))
        self._preroll: deque[np.ndarray] = deque(maxlen=n)
        self._sinks: list[Callable[[np.ndarray], None]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc = None
        self.error: str | None = None
        self.on_recover: Callable[[], None] | None = None  # called when frames flow again

    # -- sinks / preroll ---------------------------------------------------
    def add_sink(self, fn: Callable[[np.ndarray], None]) -> None:
        with self._lock:
            if fn not in self._sinks:
                self._sinks.append(fn)

    def remove_sink(self, fn: Callable[[np.ndarray], None]) -> None:
        with self._lock:
            if fn in self._sinks:
                self._sinks.remove(fn)

    def preroll(self) -> list[np.ndarray]:
        with self._lock:
            return list(self._preroll)

    def attach(self, fn: Callable[[np.ndarray], None]) -> list[np.ndarray]:
        """Atomically add a sink and return the preroll frames that precede it."""
        with self._lock:
            if fn not in self._sinks:
                self._sinks.append(fn)
            return list(self._preroll)

    # -- lifecycle ---------------------------------------------------------
    def command(self) -> list[str]:
        return ["arecord", "-q", "-t", "raw", "-D", self.device, "-f", "S16_LE",
                "-r", str(self.rate), "-c", "1"]

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bmo-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)

    def _kill(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except OSError:
            pass

    def _set_error(self, error: str | None) -> None:
        if error == self.error:
            return
        self.error = error
        if error and self.publish:
            try:
                self.publish({"type": "live", "armed": False, "model": None, "error": error})
            except Exception:
                log.exception("capture publish failed")
        elif error is None and self.on_recover:
            try:
                self.on_recover()
            except Exception:
                log.exception("capture recover callback failed")

    @staticmethod
    def _read_exact(stream, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def _run(self) -> None:
        delay = self._backoff[0]
        while not self._stop.is_set():
            got_frames = False
            try:
                self._proc = self._popen(self.command(), stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=0)
                stream = self._proc.stdout
                nbytes = FRAME_SAMPLES * 2
                while not self._stop.is_set():
                    raw = self._read_exact(stream, nbytes)
                    if len(raw) < nbytes:
                        break
                    if not got_frames:
                        got_frames = True
                        delay = self._backoff[0]
                        self._set_error(None)
                    self._dispatch(np.frombuffer(raw, dtype="<i2"))
            except Exception as e:
                log.warning("Capture error: %s", e)
            finally:
                self._kill()
                self._proc = None
            if self._stop.is_set():
                break
            log.warning("arecord stopped; restarting in %.1fs", delay)
            self._set_error("Microphone unavailable")
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self._backoff[1])

    def _dispatch(self, samples: np.ndarray) -> None:
        db = float(self.cfg.microphone.gain_db)
        if db:
            scaled = samples.astype(np.float32) * (10.0 ** (db / 20.0))
            frame = np.clip(scaled, -32768, 32767).astype(np.int16)
        else:
            frame = samples.copy()
        with self._lock:
            self._preroll.append(frame)
            sinks = list(self._sinks)
        for fn in sinks:
            try:
                fn(frame)
            except Exception:
                log.exception("capture sink failed")

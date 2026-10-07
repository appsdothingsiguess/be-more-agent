"""USB microphone capture via arecord + ffmpeg (mirrors tools/bmo_test.py)."""
import logging
import math
import signal
import subprocess
import time
from pathlib import Path

from app.audio.processing import boost_and_resample, wav_duration
from app.config import MicrophoneConfig
from app.hardware import alsa

log = logging.getLogger(__name__)


class Microphone:
    def __init__(self, cfg: MicrophoneConfig, runtime_dir: Path,
                 run=subprocess.run, popen=subprocess.Popen, cards=None):
        self.cfg = cfg
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._run = run
        self._popen = popen
        self.resolved = alsa.resolve_device(cfg.device, cfg.match, cfg.fallback_device, cards)
        self.card = cfg.alsa_card if cfg.alsa_card is not None else self.resolved.card
        self.raw_path = self.runtime_dir / "mic_raw.wav"
        self.upload_path = self.runtime_dir / "mic_upload.wav"
        self._proc = None

    def _amixer(self, *args: str) -> None:
        cmd = ["amixer", "-c", str(self.card), "sset", *args]
        try:
            r = self._run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                log.warning("amixer failed (%s): %s", " ".join(cmd), (r.stderr or "").strip())
        except Exception as e:
            log.warning("amixer error (%s): %s", " ".join(cmd), e)

    def configure(self) -> None:
        if self.card is None:
            log.warning("No ALSA card known for microphone; skipping amixer setup")
            return
        if self.cfg.set_capture_gain:
            self._amixer("Mic", str(self.cfg.capture_gain))
        if self.cfg.auto_gain_control:
            self._amixer("Auto Gain Control", "on")

    @property
    def is_recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.is_recording:
            raise RuntimeError("already recording")
        self.raw_path.unlink(missing_ok=True)
        self.upload_path.unlink(missing_ok=True)
        cmd = [
            "arecord", "-D", self.resolved.device,
            "-f", "S16_LE",
            "-r", str(self.cfg.capture_rate),
            "-c", str(self.cfg.channels),
            "-d", str(int(math.ceil(self.cfg.max_seconds))),
            str(self.raw_path),
        ]
        self._proc = self._popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def _end_proc(self, sig) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.send_signal(sig)
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except OSError:
                pass
        try:
            if proc.stderr:
                err = proc.stderr.read()
                # SIGINT is our normal push-to-talk stop; arecord reports it on stderr.
                if (err and proc.returncode not in (0, -signal.SIGINT)
                        and b"Aborted by signal" not in err):
                    log.warning("arecord: %s", err.decode(errors="replace")[-300:])
                proc.stderr.close()
        except Exception:
            pass

    def stop(self) -> Path | None:
        if self._proc is None:
            return None
        self._end_proc(signal.SIGINT)
        if wav_duration(self.raw_path) < self.cfg.min_seconds:
            return None
        return boost_and_resample(self.raw_path, self.upload_path, self.cfg.gain_db,
                                  self.cfg.upload_rate, self.cfg.channels, run=self._run)

    def abort(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()

    def record(self, seconds: float) -> Path | None:
        self.start()
        time.sleep(seconds)
        return self.stop()

"""USB speaker playback via aplay. One Speaker serialises all playback."""
import logging
import random
import subprocess
import threading
from pathlib import Path

from app.config import SpeakerConfig
from app.hardware import alsa

log = logging.getLogger(__name__)


class Speaker:
    def __init__(self, cfg: SpeakerConfig, sounds_dir: Path,
                 popen=subprocess.Popen, cards=None, run=subprocess.run):
        self.cfg = cfg
        self.sounds_dir = Path(sounds_dir)
        self._popen = popen
        self._run = run
        self._cards = cards
        self.resolved = alsa.resolve_device(cfg.device, cfg.match, cfg.fallback_device, cards)
        self.card = cfg.alsa_card if cfg.alsa_card is not None else self.resolved.card
        self._lock = threading.Lock()
        self._proc = None
        self._gen = 0  # bumped by every stop()/new play to invalidate waiters

    def configure(self) -> None:
        """Set playback volume (the UACDemo speaker boots at 30%). Failures only warn."""
        if not self.cfg.set_volume:
            return
        if self.card is None:
            log.warning("No ALSA card known for speaker; skipping volume setup")
            return
        cmd = ["amixer", "-c", str(self.card), "sset", self.cfg.volume_control, self.cfg.volume, "unmute"]
        try:
            r = self._run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                log.warning("amixer failed (%s): %s", " ".join(cmd), (r.stderr or "").strip())
        except Exception as e:
            log.warning("amixer error (%s): %s", " ".join(cmd), e)

    def refresh(self) -> bool:
        """Re-detect the speaker card (it may have been replugged under a new number).
        Re-applies the volume when it moved, since a replugged speaker boots at 30%."""
        if self.cfg.device != "auto":
            return False
        try:
            r = alsa.resolve_device(self.cfg.device, self.cfg.match, self.cfg.fallback_device,
                                    self._cards)
        except Exception as e:
            log.warning("Speaker detection failed: %s", e)
            return False
        if r.device == self.resolved.device:
            return False
        log.info("Speaker device changed: %s -> %s", self.resolved.device, r.device)
        self.resolved = r
        if self.cfg.alsa_card is None:
            self.card = r.card
        if r.source == "match":
            self.configure()
        return True

    @property
    def is_playing(self) -> bool:
        p = self._proc
        return p is not None and p.poll() is None

    def _kill(self, proc) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            proc, self._proc = self._proc, None
            self._kill(proc)

    def play(self, path, *, block: bool = True) -> bool:
        self.refresh()
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._kill(self._proc)
            self._proc = None
            try:
                proc = self._popen(
                    ["aplay", "-q", "-D", self.resolved.device, str(path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                log.warning("aplay failed to start: %s", e)
                return False
            self._proc = proc
        if not block:
            return True
        rc = proc.wait()
        with self._lock:
            if self._proc is proc:
                self._proc = None
            stopped = self._gen != gen
        return rc == 0 and not stopped

    def play_bytes(self, data: bytes, path, *, block: bool = True) -> bool:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.play(path, block=block)

    def random_sound(self, category: str) -> Path | None:
        files = sorted((self.sounds_dir / f"{category}_sounds").glob("*.wav"))
        return random.choice(files) if files else None

    def play_effect(self, category: str) -> bool:
        sound = self.random_sound(category)
        return self.play(sound, block=False) if sound else False

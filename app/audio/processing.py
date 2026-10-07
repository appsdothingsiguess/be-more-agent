"""WAV helpers: ffmpeg boost/resample, duration, base64 decoding."""
import base64
import binascii
import subprocess
import wave
from pathlib import Path


class AudioError(Exception):
    pass


def boost_and_resample(src, dst, gain_db: float, rate: int, channels: int,
                       run=subprocess.run) -> Path:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src),
        "-af", f"volume={gain_db}dB",
        "-ar", str(rate),
        "-ac", str(channels),
        str(dst),
    ]
    try:
        r = run(cmd, capture_output=True, text=True)
    except OSError as e:
        raise AudioError(f"ffmpeg could not run: {e}") from e
    if r.returncode != 0:
        raise AudioError(f"ffmpeg failed (rc={r.returncode}): {(r.stderr or '')[-300:]}")
    return Path(dst)


def wav_duration(path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else 0.0
    except (wave.Error, EOFError, OSError):
        return 0.0


def is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def decode_wav_b64(s: str) -> bytes:
    try:
        data = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"invalid base64 audio: {e}") from e
    if not is_wav(data):
        raise ValueError("decoded audio is not a RIFF/WAVE file")
    return data

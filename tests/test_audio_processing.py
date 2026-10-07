import base64
import wave

import pytest

from app.audio.processing import (AudioError, boost_and_resample, decode_wav_b64,
                                  is_wav, wav_duration)
from tests.wavutil import make_wav, peak


def test_boost(tmp_path):
    src, dst = tmp_path / "a.wav", tmp_path / "b.wav"
    make_wav(src)
    boost_and_resample(src, dst, 12, 16000, 1)
    with wave.open(str(dst), "rb") as w:
        assert (w.getframerate(), w.getnchannels()) == (16000, 1)
    assert peak(dst) > peak(src) * 2
    assert abs(wav_duration(dst) - 0.5) < 0.05


def test_boost_error(tmp_path):
    (tmp_path / "bad.wav").write_bytes(b"junk")
    with pytest.raises(AudioError):
        boost_and_resample(tmp_path / "bad.wav", tmp_path / "o.wav", 1, 16000, 1)


def test_duration_unreadable(tmp_path):
    p = tmp_path / "x.wav"
    p.write_bytes(b"RIFF")
    assert wav_duration(p) == 0.0
    assert wav_duration(tmp_path / "missing.wav") == 0.0


def test_decode(tmp_path):
    p = tmp_path / "a.wav"
    make_wav(p, secs=0.05)
    data = p.read_bytes()
    assert is_wav(data)
    assert decode_wav_b64(base64.b64encode(data).decode()) == data
    with pytest.raises(ValueError):
        decode_wav_b64("!!notbase64!!")
    with pytest.raises(ValueError):
        decode_wav_b64(base64.b64encode(b"hello world, not wav").decode())

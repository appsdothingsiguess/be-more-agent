import numpy as np

from app.audio.endpoint import Endpointer
from app.audio.vad import EnergyVAD, make_vad


def run(ep, flags):
    return [ep.feed(f) for f in flags]


def test_onset_then_end():
    ep = Endpointer(0.1, 0.3, 5, 30)
    out = run(ep, [False, True, True, False, False, False])
    assert out == [None, "onset", None, None, None, "end"]
    assert ep.feed(True) is None  # latched


def test_speech_resets_silence():
    ep = Endpointer(0.1, 0.3, 5, 30)
    out = run(ep, [True, False, False, True, False, False, False])
    assert out == ["onset", None, None, None, None, None, "end"]


def test_timeout_without_speech():
    ep = Endpointer(0.1, 0.3, 0.5, 30)
    assert run(ep, [False] * 5) == [None] * 4 + ["timeout"]


def test_max_seconds():
    ep = Endpointer(0.1, 0.3, 5, 0.4)
    assert run(ep, [True] * 4) == ["onset", None, None, "max"]


def test_energy_vad_detects_loud_over_quiet():
    vad = EnergyVAD()
    rng = np.random.default_rng(1)
    quiet = (rng.standard_normal(1280) * 20).astype(np.int16)
    loud = (rng.standard_normal(1280) * 4000).astype(np.int16)
    assert not any(vad.is_speech(quiet) for _ in range(10))
    assert vad.is_speech(loud)
    vad.reset()
    assert vad.floor is None


def test_make_vad_falls_back_to_energy(monkeypatch):
    import app.audio.vad as v

    def boom(*a, **k):
        raise RuntimeError("no model")
    monkeypatch.setattr(v, "SileroVAD", boom)
    assert isinstance(make_vad(type("L", (), {"vad": "silero"})()), EnergyVAD)


def test_short_blip_does_not_end_the_turn():
    ep = Endpointer(0.1, 0.3, 1.0, 30, min_speech=0.2)
    out = run(ep, [True, False, False, False, False])     # 0.1 s blip, then silence
    assert "end" not in out
    assert run(ep, [False] * 5)[-1] == "timeout"          # still gives up on time


def test_real_speech_after_a_blip_ends_normally():
    ep = Endpointer(0.1, 0.3, 5, 30, min_speech=0.2)
    out = run(ep, [True, False, False, False, True, True, True, False, False, False])
    assert out[0] == "onset" and out[4] == "onset" and out[-1] == "end"

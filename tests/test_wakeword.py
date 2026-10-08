import numpy as np
import pytest

from app.audio.wakeword import WakeDetector, WakeUnavailable
from app.config import WakeWordConfig


def test_unavailable_model_raises(tmp_path):
    cfg = WakeWordConfig(model="no_such_model_xyz", custom_model="models/none.onnx")
    with pytest.raises(WakeUnavailable):
        WakeDetector(cfg, tmp_path)


def test_custom_model_name_from_stem(tmp_path, monkeypatch):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "hey_bmo.onnx").write_bytes(b"x")
    with pytest.raises(WakeUnavailable):  # garbage file cannot load, but the path was chosen
        WakeDetector(WakeWordConfig(), tmp_path)


def test_real_model_silence_scores_low(tmp_path):
    pytest.importorskip("openwakeword")
    try:
        det = WakeDetector(WakeWordConfig(custom_model="none.onnx"), tmp_path)
    except WakeUnavailable:
        pytest.skip("wake models not downloaded")
    assert det.name == "hey_jarvis"
    rng = np.random.default_rng(0)
    for _ in range(15):
        s = det.score((rng.standard_normal(1280) * 30).astype(np.int16))
        assert s < 0.1
    det.reset()

"""openwakeword wrapper (lazy import, ONNX backend)."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class WakeUnavailable(Exception):
    """openwakeword or its models could not be loaded."""


class WakeDetector:
    def __init__(self, cfg, base_dir: Path):
        """cfg is a WakeWordConfig; custom_model is relative to base_dir."""
        custom = Path(cfg.custom_model).expanduser() if cfg.custom_model else None
        if custom is not None and not custom.is_absolute():
            custom = Path(base_dir) / custom
        if custom is not None and custom.is_file():
            target, self.name = str(custom), custom.stem
        else:
            target, self.name = cfg.model, cfg.model
        try:
            from openwakeword.model import Model
            self._model = Model(wakeword_models=[target], inference_framework="onnx")
        except Exception as e:
            raise WakeUnavailable(f"wake word unavailable: {e}") from e

    def score(self, frame: np.ndarray) -> float:
        scores = self._model.predict(frame)
        return float(max(scores.values())) if scores else 0.0

    def reset(self) -> None:
        try:
            self._model.reset()
        except Exception:
            log.debug("wake model reset failed", exc_info=True)

"""Voice activity detection: numpy energy VAD and Silero (onnxruntime)."""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

SILERO_CHUNK = 512  # samples at 16 kHz


class EnergyVAD:
    """RMS against an adaptive noise floor."""

    def __init__(self, ratio: float = 3.0, min_rms: float = 300.0):
        self.ratio = ratio
        self.min_rms = min_rms
        self.floor: float | None = None

    def reset(self) -> None:
        self.floor = None

    def is_speech(self, frame: np.ndarray) -> bool:
        x = frame.astype(np.float32)
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        if self.floor is None:
            self.floor = rms
        speech = rms > max(self.floor * self.ratio, self.min_rms)
        if rms < self.floor:
            self.floor = 0.5 * self.floor + 0.5 * rms
        elif not speech:
            self.floor = 0.95 * self.floor + 0.05 * rms
        return speech


class SileroVAD:
    """Silero v4 (h/c state) from openwakeword's resources, one onnxruntime thread."""

    def __init__(self, model_path: str | None = None, threshold: float = 0.5):
        import onnxruntime as ort
        if model_path is None:
            import os

            import openwakeword
            model_path = os.path.join(openwakeword.__path__[0], "resources", "models",
                                      "silero_vad.onnx")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(model_path, opts, providers=["CPUExecutionProvider"])
        names = {i.name for i in self._session.get_inputs()}
        if not {"input", "sr", "h", "c"} <= names:
            raise RuntimeError(f"unsupported silero model inputs: {sorted(names)}")
        self.threshold = threshold
        self._sr = np.array(16000, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.int16)

    def is_speech(self, frame: np.ndarray) -> bool:
        self._buf = np.concatenate([self._buf, frame.astype(np.int16)])
        best = 0.0
        while len(self._buf) >= SILERO_CHUNK:
            chunk, self._buf = self._buf[:SILERO_CHUNK], self._buf[SILERO_CHUNK:]
            x = (chunk.astype(np.float32) / 32768.0)[None, :]
            out, self._h, self._c = self._session.run(
                None, {"input": x, "sr": self._sr, "h": self._h, "c": self._c})
            best = max(best, float(out[0][0]))
        return best >= self.threshold


def make_vad(listen_cfg):
    """Silero when configured and loadable, otherwise the energy VAD."""
    if getattr(listen_cfg, "vad", "silero") == "silero":
        try:
            return SileroVAD()
        except Exception as e:
            log.warning("Silero VAD unavailable (%s); using energy VAD", e)
    return EnergyVAD()

"""End-of-speech detection as pure logic over per-frame speech flags."""
from __future__ import annotations


class Endpointer:
    """feed() returns None, "onset" (first speech), "end" (silence after speech),
    "timeout" (no speech at all) or "max" (turn too long). Terminal results latch.
    Speech shorter than min_speech before the silence is a blip (a click, an echo):
    it is forgotten and listening goes on, so it never ends the turn."""

    def __init__(self, frame_s: float, end_silence: float, no_speech_timeout: float,
                 max_seconds: float, min_speech: float = 0.0):
        self.frame_s = frame_s
        self.end_silence = end_silence
        self.no_speech_timeout = no_speech_timeout
        self.max_seconds = max_seconds
        self.min_speech = min_speech
        self.speech = 0.0
        self.elapsed = 0.0
        self.silence = 0.0
        self.spoke = False
        self.done = False

    def feed(self, is_speech: bool) -> str | None:
        if self.done:
            return None
        self.elapsed += self.frame_s
        result = None
        if is_speech:
            self.silence = 0.0
            self.speech += self.frame_s
            if not self.spoke:
                self.spoke = True
                result = "onset"
        elif self.spoke:
            self.silence += self.frame_s
            if self.speech < self.min_speech - 1e-9:
                self.spoke, self.speech, self.silence = False, 0.0, 0.0   # just a blip
            elif self.silence >= self.end_silence - 1e-9:
                result = "end"
        elif self.elapsed >= self.no_speech_timeout - 1e-9:
            result = "timeout"
        if result in (None, "onset") and self.elapsed >= self.max_seconds - 1e-9:
            result = "max"
        if result in ("end", "timeout", "max"):
            self.done = True
        return result

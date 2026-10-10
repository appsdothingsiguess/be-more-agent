"""Face animation logic: which expression to show, blinking, eye drift and lip-sync.

No Tk here. FaceAnimator.tick(now) advances the face; the GUI draws face.draw_ops().
"""
from __future__ import annotations

import array
import io
import math
import random
import wave
from dataclasses import dataclass
from pathlib import Path

from app.ui import face_svg
from app.ui.face_svg import Face, MOUTH_PARTS
from app.ui.states import BotState

STATE_EXPRESSION = {
    BotState.WARMUP: "sleepy",
    BotState.IDLE: "neutral",
    BotState.LISTENING: "listening",
    BotState.CAPTURING: "surprised",
    BotState.THINKING: "thinking",
    BotState.SPEAKING: "neutral",
    BotState.ERROR: "error",
}
# Expressions with plain round eyes: the only ones that blink.
BLINKABLE = {"neutral", "happy", "sad", "surprised", "confused", "angry", "thinking",
             "listening", "awestruck", "kiss", "chewing"}
EMOTION_STATES = (BotState.IDLE, BotState.SPEAKING)
# Eye wander in SVG units (x, y) per state.
DRIFT = {BotState.IDLE: (16, 7), BotState.LISTENING: (6, 3), BotState.SPEAKING: (7, 3)}

EMOTION_HOLD_S = 8.0       # an emotion lingers this long once BMO stops talking
SLEEP_AFTER_S = 180.0      # idle this long and BMO nods off
SPEECH_LAG_S = 0.06        # aplay starts a moment after we flip to SPEAKING
ENV_WINDOW_S = 0.04
RATE_CALM, RATE_FAST = 11.0, 30.0   # 1/s; how quickly the face chases its target
RATE_TALK = 12.0                    # mouth morph speed while speaking (blinks stay fast)
MOUTH_HOLD_S = 0.20                 # a mouth shape stays at least this long
SMOOTH_WINDOWS = 4                  # loudness is averaged over ~160 ms
FLAP_S = 0.22                       # random flapping when there is no audio data


def envelope_from_wav(path, window_s: float = ENV_WINDOW_S) -> tuple[list, float]:
    """Loudness per window of a 16-bit WAV (a path, or the file's bytes), scaled 0..1.
    Returns ([], window) if unreadable."""
    try:
        with wave.open(io.BytesIO(path) if isinstance(path, bytes) else str(path), "rb") as w:
            if w.getsampwidth() != 2:
                return [], window_s
            ch, rate = w.getnchannels(), w.getframerate()
            data = array.array("h")
            data.frombytes(w.readframes(w.getnframes()))
    except (OSError, EOFError, wave.Error):
        return [], window_s
    if ch > 1:
        data = data[::ch]
    n = max(1, int(rate * window_s))
    levels = []
    for i in range(0, len(data), n):
        chunk = data[i:i + n:2] or data[i:i + n]
        if not chunk:
            break
        levels.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)))
    if not levels:
        return [], window_s
    ranked = sorted(levels)
    ref = ranked[int(len(ranked) * 0.92)] or ranked[-1] or 1.0
    floor = ref * 0.06
    levels = [min(1.0, lv / ref) if lv > floor else 0.0 for lv in levels]
    k = SMOOTH_WINDOWS
    smooth = [sum(levels[max(0, i - k + 1):i + 1]) / k if i >= k - 1 else
              sum(levels[:i + 1]) / (i + 1) for i in range(len(levels))]
    return smooth, window_s


# Extra mouth shapes mixed in per loudness band, for variety (loudness cannot tell vowels apart).
MOUTH_VARIANTS = {
    "mouth_small": ("mouth_small", "mouth_pucker", "mouth_teeth"),
    "mouth_open": ("mouth_open", "mouth_open", "mouth_o", "mouth_ee"),
    "mouth_wide": ("mouth_wide", "mouth_wide", "mouth_tall"),
}
# Not emotions: driven by state, by blinking, or by speech.
NOT_EMOTIONS = ("blink", "error", "listening")

@dataclass(frozen=True)
class Move:
    """A server "expression" action: a short animation played once."""
    seconds: float
    face: str | None = None     # expression shown while it runs (else the current one)
    mouth: str | None = None    # mouth shape held while it runs
    eyes: str | None = None     # blink | wink | heart | star
    motion: str | None = None   # whole-face or eye movement, see _move_offset


MOVES = {
    "blink": Move(0.4, eyes="blink"),
    "wink": Move(0.7, eyes="wink"),
    "laugh": Move(1.2, face="happy", mouth="mouth_wide", motion="laugh"),
    "look_around": Move(1.5, motion="look"),
    "nod": Move(1.0, motion="nod"),
    "shake_head": Move(1.0, motion="shake"),
    "bounce": Move(0.8, motion="hop"),
    "wiggle": Move(1.2, motion="wiggle"),
    "sparkle_eyes": Move(1.5, face="excited", eyes="star"),
    "heart_eyes": Move(1.5, face="happy", eyes="heart"),
    "yawn": Move(1.5, face="sleepy", mouth="mouth_tall"),
    "dance": Move(2.0, motion="dance"),
}
MOVE_ALIASES = {"spin": "dance", "boogie": "dance", "jump": "bounce", "hop": "bounce",
                "shake": "shake_head", "look": "look_around", "sparkle": "sparkle_eyes",
                "hearts": "heart_eyes"}
FACE_ACTION_S = 1.0         # a "face" action holds its emotion this long
OVERLAYS = ("song", "music")
LYRIC_FALLBACK_S = 3.0      # seconds per lyric line when the reply length is unknown


def move_for(name) -> tuple[str, Move] | None:
    key = name.strip().lower() if isinstance(name, str) else ""
    key = MOVE_ALIASES.get(key, key)
    return (key, MOVES[key]) if key in MOVES else None


def emotion_names(faces: dict) -> list:
    """Every expression BMO may pick for itself, in file order (for the server's tool schema)."""
    return [n for n in faces if not n.startswith("mouth_") and n not in NOT_EMOTIONS]


def mouth_for_level(level: float) -> str:
    if level < 0.10:
        return "mouth_closed"
    if level < 0.30:
        return "mouth_small"
    if level < 0.60:
        return "mouth_open"
    return "mouth_wide"


class FaceAnimator:
    def __init__(self, faces: dict, rng: random.Random | None = None):
        if "neutral" not in faces:
            raise ValueError("face set needs a 'neutral' expression")
        self.faces = faces
        self.rng = rng or random.Random()
        self.state = BotState.WARMUP
        self.emotion: str | None = None
        self.emotion_until = 0.0
        self.state_since = 0.0
        self.envelope: list = []
        self.window = ENV_WINDOW_S
        self.speech_start: float | None = None
        self._pending_envelope: tuple | None = None
        self._mouth = "mouth_closed"
        self._band = "mouth_closed"
        self._flap_next = 0.0
        self._mouth_since = 0.0
        self._blink_until = 0.0
        self._next_blink = 2.0
        self._drift = (0.0, 0.0)
        self._drift_goal = (0.0, 0.0)
        self._next_drift = 1.0
        self._move: str | None = None
        self._move_start = self._move_end = 0.0
        self.overlay: str | None = None     # song | music: notes (and lyrics) over the face
        self.lyrics: list = []
        # Seconds into the speech where each line starts (a streamed song); None = spread
        # the lines evenly over the audio.
        self.lyric_starts: list | None = None
        self.caption = ""                   # status text while BMO works
        self._shown: str | None = None      # face action, any state
        self._shown_until = 0.0
        self._last = None
        self._target_key = None
        self._first = True
        self.target = faces["neutral"].copy()
        self.current = faces["neutral"].copy()
        self.dirty = True

    # ----- inputs (call from the UI thread)

    def set_state(self, state: BotState, now: float) -> None:
        if state == self.state:
            return
        self.state = state
        self.state_since = now
        if state == BotState.SPEAKING:
            env = self._pending_envelope
            self._pending_envelope = None
            self.envelope, self.window = env if env else ([], ENV_WINDOW_S)
            self.speech_start = now
            self._mouth = self._band = "mouth_closed"
        else:
            if self.speech_start is not None and state != BotState.SPEAKING:
                self.emotion_until = now + EMOTION_HOLD_S
            self.speech_start = None
        if state not in EMOTION_STATES:
            self.emotion = None
        if state != BotState.THINKING:
            self.caption = ""
        if state in (BotState.IDLE, BotState.ERROR, BotState.LISTENING):
            self.overlay, self.lyrics, self.lyric_starts = None, [], None

    def set_emotion(self, name: str | None, now: float) -> None:
        if name is not None and name not in self.faces:
            return
        self.emotion = name
        self.emotion_until = now + 60.0 if self.state == BotState.SPEAKING else now + EMOTION_HOLD_S

    def play_expression(self, name: str, now: float) -> float:
        """A server 'expression' action: a move (dance, wink, ...) or a face name.
        Returns how long it lasts in seconds; 0.0 if BMO has no such expression."""
        found = move_for(name)
        if found:
            key, move = found
            self._move, self._move_start, self._move_end = key, now, now + move.seconds
            self.dirty = True
            return move.seconds
        key = name.strip().lower() if isinstance(name, str) else ""
        if key in self.faces and key in emotion_names(self.faces):
            self.set_emotion(key, now)
            return FACE_ACTION_S
        return 0.0

    def show_face(self, name: str, now: float, seconds: float = FACE_ACTION_S) -> bool:
        """A server 'face' action: hold an emotion briefly, as one step of a face show."""
        key = name.strip().lower() if isinstance(name, str) else ""
        if key not in self.faces or key not in emotion_names(self.faces):
            return False
        # Own slot, not self.emotion: it must show while THINKING too (streamed actions).
        self._shown, self._shown_until = key, now + seconds
        self.dirty = True
        return True

    def clear(self) -> None:
        """The user cut BMO off: drop the running move, face action, emotion and notes, so
        the plain face shows at once instead of lingering for seconds."""
        self._move, self._shown, self.emotion = None, None, None
        self.overlay, self.lyrics, self.lyric_starts = None, [], None
        self.dirty = True

    def set_overlay(self, kind: str | None, lyrics=()) -> None:
        """Music notes over the face: 'song' (with lyrics), 'music', or None to clear."""
        self.overlay = kind if kind in OVERLAYS else None
        self.lyrics = [ln.strip() for ln in lyrics if ln.strip()] if self.overlay == "song" else []
        self.dirty = True

    def set_caption(self, text: str) -> None:
        self.caption = text or ""
        self.dirty = True

    def time_lyrics(self) -> None:
        """A streamed song: no words until add_lyric_start() says when the first line is."""
        self.lyric_starts = []

    def add_lyric_start(self, seconds: float) -> None:
        """A streamed song: the next lyric line starts this many seconds into the audio."""
        if self.lyric_starts is None:
            self.lyric_starts = []
        self.lyric_starts.append(seconds)

    def lyric(self, now: float) -> str:
        """The lyric line for now: at its start time when known (a streamed song has one
        piece per line), else the lines spread evenly over the reply audio."""
        if not self.lyrics or self.state != BotState.SPEAKING or self.speech_start is None:
            return ""
        n, elapsed = len(self.lyrics), max(0.0, now - self.speech_start)
        if self.lyric_starts is not None:
            i = sum(1 for t in self.lyric_starts[:n] if t <= elapsed)
            return self.lyrics[i - 1] if i else ""
        total = len(self.envelope) * self.window
        per = total / n if total > 0 else LYRIC_FALLBACK_S
        return self.lyrics[min(n - 1, int(elapsed / per))]

    def speech_level(self, now: float) -> float:
        level = self._speech_level(now)
        return 0.5 if level is None else level

    def _active_move(self, now: float) -> Move | None:
        return MOVES[self._move] if self._move is not None and now < self._move_end else None

    def _move_offset(self, now: float) -> tuple[float, float]:
        """Whole-face shift in SVG units for the running move; eases in and out."""
        if self._move is None or now >= self._move_end:
            return (0.0, 0.0)
        dur = self._move_end - self._move_start
        t = now - self._move_start
        env = math.sin(math.pi * t / dur)
        motion = MOVES[self._move].motion
        if motion == "dance":
            return (38 * math.sin(2 * math.pi * 1.5 * t) * env,
                    -20 * abs(math.sin(2 * math.pi * 1.5 * t)) * env)
        if motion == "hop":
            return (0.0, -70 * env)
        if motion == "nod":
            return (0.0, 24 * math.sin(2 * math.pi * 2 * t / dur) * env)
        if motion == "shake":
            return (28 * math.sin(2 * math.pi * 3 * t / dur) * env, 0.0)
        if motion == "wiggle":
            return (34 * math.sin(2 * math.pi * 2 * t / dur) * env, 0.0)
        if motion == "laugh":
            return (0.0, -14 * abs(math.sin(2 * math.pi * 3 * t / dur)) * env)
        return (0.0, 0.0)

    def _look_offset(self, now: float) -> tuple[float, float]:
        """Eyes only: glance left, then right, then back (look_around)."""
        move = self._active_move(now)
        if move is None or move.motion != "look":
            return (0.0, 0.0)
        t = (now - self._move_start) / (self._move_end - self._move_start)
        return (-44 * math.sin(2 * math.pi * t), 0.0)

    def prepare_speech(self, envelope: list, window: float = ENV_WINDOW_S) -> None:
        """Hand over the reply's loudness just before set_state(SPEAKING)."""
        self._pending_envelope = (envelope, window)
        self.lyric_starts = None

    def append_speech(self, envelope: list) -> None:
        """Streamed reply audio: the next piece's loudness, played right after the last."""
        if self.state == BotState.SPEAKING:
            self.envelope = self.envelope + list(envelope)
        elif self._pending_envelope is not None:
            env, window = self._pending_envelope
            self._pending_envelope = (env + list(envelope), window)

    # ----- per-frame

    def _expression(self, now: float) -> str:
        if self.emotion and now > self.emotion_until and self.state != BotState.SPEAKING:
            self.emotion = None
        move = self._active_move(now)
        if move is not None and move.face in self.faces:
            return move.face
        if self._shown is not None:
            if now < self._shown_until:
                return self._shown
            self._shown = None
        if self.state in EMOTION_STATES and self.emotion in self.faces:
            return self.emotion
        if self.state == BotState.IDLE and now - self.state_since > SLEEP_AFTER_S \
                and "sleepy" in self.faces:
            return "sleepy"
        return STATE_EXPRESSION[self.state] if STATE_EXPRESSION[self.state] in self.faces \
            else "neutral"

    def _speech_level(self, now: float) -> float | None:
        """Loudness 0..1 now, or None if there is no envelope to follow."""
        if not self.envelope or self.speech_start is None:
            return None
        i = int((now - self.speech_start - SPEECH_LAG_S) / self.window)
        return self.envelope[i] if 0 <= i < len(self.envelope) else 0.0

    def _pick_mouth(self, now: float) -> str:
        level = self._speech_level(now)
        if level is None:                       # no audio data: flap at random like the old faces
            if now >= self._flap_next:
                self._flap_next = now + FLAP_S
                self._mouth = self.rng.choice(["mouth_closed", "mouth_small", "mouth_open",
                                               "mouth_wide", "mouth_o", "mouth_ee",
                                               "mouth_pucker", "mouth_tall", "mouth_teeth"])
            return self._mouth
        band = mouth_for_level(level)
        if band != self._band and now - self._mouth_since >= MOUTH_HOLD_S:
            self._mouth_since = now
            self._band = band
            self._mouth = self.rng.choice(MOUTH_VARIANTS.get(band, (band,)))
            if self._mouth not in self.faces:
                self._mouth = band
        return self._mouth

    def _retarget(self, expr: str, mouth: str | None, blinking: bool,
                  wink: bool = False, eyes: str | None = None) -> None:
        key = (expr, mouth, blinking, wink, eyes)
        if key == self._target_key:
            return
        self._target_key = key
        tgt = self.faces[expr].copy()
        if mouth and mouth in self.faces:
            src = self.faces[mouth]
            for part in MOUTH_PARTS:
                tgt.parts.pop(part, None)
                if part in src.parts:
                    tgt.parts[part] = [s.copy() for s in src.parts[part]]
        if blinking and "blink" in self.faces:
            for part in ("eye-left", "eye-right"):
                tgt.parts[part] = [s.copy() for s in self.faces["blink"].parts[part]]
        if wink and "blink" in self.faces and "eye-right" in self.faces["blink"].parts:
            tgt.parts["eye-right"] = [s.copy() for s in self.faces["blink"].parts["eye-right"]]
        if eyes in ("heart", "star"):
            make = face_svg.heart_shape if eyes == "heart" else face_svg.star_shape
            for part in ("eye-left", "eye-right"):
                shapes = tgt.parts.get(part) or self.faces["neutral"].parts.get(part)
                if shapes:
                    cx, cy = shapes[0].centroid()
                    tgt.parts[part] = [make(cx, cy, 52.0)]
        face_svg.pad_pair(self.current, tgt)
        self.target = tgt
        if self._first:
            face_svg.snap(self.current, tgt)
            self._first = False

    def tick(self, now: float) -> bool:
        """Advance to time now (monotonic seconds). True if the picture changed."""
        dt = 0.033 if self._last is None else max(0.0, min(0.25, now - self._last))
        expr = self._expression(now)
        move = self._active_move(now)
        eyes = move.eyes if move is not None else None

        blinking = False
        if eyes == "blink":
            blinking = now - self._move_start < move.seconds / 2
        elif eyes in ("heart", "star"):
            pass                                # no blinking over generated eyes
        elif expr in BLINKABLE:
            if now >= self._next_blink:
                self._blink_until = now + 0.13
                self._next_blink = now + self.rng.uniform(2.2, 6.0)
                if self.rng.random() < 0.15:    # now and then, a quick double blink
                    self._next_blink = now + 0.35
            blinking = now < self._blink_until

        mouth = None
        if self.state == BotState.SPEAKING:
            self._mouth = self._pick_mouth(now)
            mouth = self._mouth
        if move is not None and move.mouth in self.faces:
            mouth = move.mouth
        moving = self._move is not None
        if moving and now >= self._move_end:
            self._move, moving = None, False
            self.dirty = True                   # one last frame back at rest
        self._retarget(expr, mouth, blinking, wink=eyes == "wink",
                       eyes=eyes if eyes in ("heart", "star") else None)

        moved = self._update_drift(now, dt) or moving or self.overlay is not None
        rate = RATE_FAST if blinking else RATE_TALK if mouth else RATE_CALM
        delta = face_svg.approach(self.current, self.target, 1 - math.exp(-rate * dt))
        self._last = now
        changed = delta > 0.04 or moved or self.dirty
        self.dirty = False
        return changed

    def _update_drift(self, now: float, dt: float) -> bool:
        rx, ry = DRIFT.get(self.state, (0, 0))
        if now >= self._next_drift:
            self._next_drift = now + self.rng.uniform(1.2, 3.8)
            if self.rng.random() < 0.4:         # look back to centre fairly often
                self._drift_goal = (0.0, 0.0)
            else:
                self._drift_goal = (self.rng.uniform(-rx, rx), self.rng.uniform(-ry, ry))
        if not rx:
            self._drift_goal = (0.0, 0.0)
        k = 1 - math.exp(-9 * dt)
        nx = self._drift[0] + (self._drift_goal[0] - self._drift[0]) * k
        ny = self._drift[1] + (self._drift_goal[1] - self._drift[1]) * k
        moved = abs(nx - self._drift[0]) + abs(ny - self._drift[1]) > 0.03
        self._drift = (nx, ny)
        return moved

    def offsets(self, now: float | None = None) -> dict:
        now = self._last if now is None else now
        mx, my = self._move_offset(now) if now is not None else (0.0, 0.0)
        out = {name: (mx, my) for name in self.current.parts}
        dx, dy = self._drift
        lx, ly = self._look_offset(now) if now is not None else (0.0, 0.0)
        dx, dy = dx + lx, dy + ly
        for name in ("eye-left", "eye-right", "brow-left", "brow-right"):
            out[name] = (dx + mx, dy + my)
        return out

    def ops(self, scale_x: float, scale_y: float) -> list:
        return face_svg.draw_ops(self.current, scale_x, scale_y, self.offsets())

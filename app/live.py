"""Hands-free listening: wake word, VAD auto-stop and follow-up turns.

A worker thread consumes frames from AudioCapture (bounded queue, oldest
dropped when behind) and drives the InteractionController through its public
API only. Modes:
  ARMED   scoring frames for the wake word while the controller is idle
  TURN    controller is LISTENING; the Endpointer decides when to stop
  MUTED   controller busy (thinking/capturing/speaking/Pi playback) plus a tail
Follow-up is a TURN started by us after a Pi-spoken, successful, non-web turn.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

from app.audio.endpoint import Endpointer
from app.audio.wakeword import WakeUnavailable
from app.ui.states import BotState

log = logging.getLogger(__name__)

ARMED, TURN, MUTED = "armed", "turn", "muted"
BUSY = (BotState.THINKING, BotState.CAPTURING, BotState.SPEAKING)
QUEUE_FRAMES = 25  # ~2 s


def pretty_name(name: str) -> str:
    words = [w for w in name.replace("-", "_").split("_") if w]
    return " ".join(w.upper() if w.lower() == "bmo" else w.capitalize() for w in words)


class LiveListener:
    def __init__(self, cfg, controller, capture, detector_factory: Callable, vad_factory: Callable,
                 clock=time.monotonic):
        self.cfg = cfg
        self.controller = controller
        self.capture = capture
        self._detector_factory = detector_factory
        self._vad_factory = vad_factory
        self._clock = clock
        self._frames: queue.Queue = queue.Queue(maxsize=QUEUE_FRAMES)
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._unsubscribe = None

        self._detector = None
        self._vad = None
        self._wake_error: str | None = None
        self._dirty = True
        self._published: tuple | None = None
        self._mode = ARMED
        self._endpointer: Endpointer | None = None
        self._turn_source: str | None = None
        self._mute_until: float | None = None
        self._followup_pending = False
        self._last_wake = -1e9

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._unsubscribe = self.controller.subscribe(self._on_event)
        self.capture.on_recover = self._on_recover
        self.capture.add_sink(self._on_frame)
        self._thread = threading.Thread(target=self._run, name="bmo-live", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.capture.remove_sink(self._on_frame)
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)

    # -- inputs (any thread) -----------------------------------------------
    def _on_frame(self, frame) -> None:
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frames.put_nowait(frame)
            except queue.Full:
                pass

    def _on_event(self, event: dict) -> None:
        if event.get("type") in ("setting", "turn_done"):
            self._events.put(event)

    def _on_recover(self) -> None:
        self._published = None
        self._dirty = True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self._frames.get(timeout=0.2)
            except queue.Empty:
                frame = None
            try:
                self.step(frame)
            except Exception:
                log.exception("Live listener step failed")

    # -- logic (worker thread; tests call step() directly) -----------------
    def step(self, frame=None) -> None:
        self._drain_events()
        if self._dirty:
            self._dirty = False
            self._apply_config()
        now = self._clock()
        state = self.controller.state
        if self._busy(state):
            if self._mute_until is not None:  # a new turn began after the tail started
                self._followup_pending = False
            self._mode, self._mute_until, self._endpointer = MUTED, None, None
            return
        if self._mode == MUTED:
            if self._mute_until is None:
                self._mute_until = now + self.cfg.listen.mute_tail_seconds
            if now < self._mute_until:
                return
            self._mute_until = None
            self._mode = ARMED
            if self._detector is not None:
                self._detector.reset()
            if self._followup_pending:
                self._followup_pending = False
                self._begin_followup(now)
                state = self.controller.state
        if self._mode == ARMED and state is BotState.LISTENING:
            self._adopt_listening()
        elif self._mode == TURN and state is not BotState.LISTENING:
            self._mode, self._endpointer = ARMED, None  # button stopped it, or max hit
            if self._detector is not None:
                self._detector.reset()
        if frame is None:
            return
        if self._mode == TURN:
            self._turn_frame(frame)
        elif self._mode == ARMED:
            self._armed_frame(frame, now, state)

    def _busy(self, state) -> bool:
        if state in BUSY:
            return True
        speaker = getattr(self.controller, "speaker", None)
        return bool(getattr(speaker, "is_playing", False))

    def _drain_events(self) -> None:
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                return
            if ev["type"] == "setting":
                key = str(ev.get("key", ""))
                if key == "wake_word.enabled" or key.startswith("listen.followup"):
                    self._dirty = True
            elif (ev.get("ok") and ev.get("spoke_on_pi") and ev.get("source") != "web"
                  and self.cfg.listen.followup):
                self._followup_pending = True
                if self._mode != MUTED:  # speech already over: just the tail
                    self._mode, self._mute_until = MUTED, None

    def _armed_frame(self, frame, now: float, state) -> None:
        det = self._detector
        if det is None or state not in (BotState.IDLE, BotState.ERROR):
            return
        if det.score(frame) < self.cfg.wake_word.threshold:
            return
        if now - self._last_wake < self.cfg.wake_word.cooldown_seconds:
            return
        self._last_wake = now
        self.capture.skip_preroll = True
        if not self.controller.start_listening("wake"):
            self.capture.skip_preroll = False
            return
        log.info("Wake word detected")
        if self.cfg.listen.prewarm_on_wake:
            self.controller.prewarm()
        self._begin_turn("wake", self.cfg.listen.no_speech_timeout)

    def _begin_followup(self, now: float) -> None:
        if self.controller.state not in (BotState.IDLE, BotState.ERROR):
            return
        if self.controller.start_listening("followup"):
            self._begin_turn("followup", self.cfg.listen.followup_seconds)

    def _adopt_listening(self) -> None:
        source = getattr(self.controller, "listening_source", None)
        if source == "button" and self.cfg.listen.auto_stop_button:
            self._begin_turn("button", self.cfg.listen.no_speech_timeout)

    def _begin_turn(self, source: str, no_speech: float) -> None:
        if self._vad is None:
            self._vad = self._vad_factory()
        self._vad.reset()
        self._turn_source = source
        self._mode = TURN
        self._endpointer = Endpointer(self.capture.frame_s, self.cfg.listen.end_silence_seconds,
                                      no_speech, self.cfg.microphone.max_seconds)

    def _turn_frame(self, frame) -> None:
        result = self._endpointer.feed(self._vad.is_speech(frame))
        if result in ("end", "max"):
            self._mode, self._endpointer = ARMED, None
            self.controller.finish_listening()
        elif result == "timeout":
            self._mode, self._endpointer = ARMED, None
            self.controller.cancel_listening(quiet=True)
        if result in ("end", "max", "timeout") and self._detector is not None:
            self._detector.reset()

    # -- config / publishing -----------------------------------------------
    def _apply_config(self) -> None:
        if self.cfg.wake_word.enabled:
            if self._detector is None and self._wake_error is None:
                try:
                    self._detector = self._detector_factory()
                except WakeUnavailable as e:
                    self._wake_error = str(e)
                except Exception as e:
                    log.exception("Wake detector failed")
                    self._wake_error = f"wake word unavailable: {e}"
        else:
            self._detector, self._wake_error = None, None
        self._publish_state()

    def _publish_state(self) -> None:
        mic_error = getattr(self.capture, "error", None)
        error = mic_error or self._wake_error
        armed = self._detector is not None and not mic_error
        model = getattr(self._detector, "name", None) if self._detector is not None else None
        followup = bool(self.cfg.listen.followup)
        state = (armed, model, error, followup)
        if armed and model:
            hint = f"Say '{pretty_name(model)}'"
        else:
            hint = "Press Start to talk"
        self.controller.idle_hint = hint
        if state != self._published:
            self._published = state
            self.controller.publish({"type": "live", "armed": armed, "model": model,
                                     "error": error, "followup": followup})

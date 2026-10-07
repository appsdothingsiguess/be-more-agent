"""Interaction state machine: record -> (camera) -> server -> speak, with interruption.

One interaction runs at a time on a worker thread. Every turn captures the
current ``generation``; an interrupt bumps it, so any late response from an
interrupted turn is dropped and never played. Interrupt order (per the server
contract): stop the speaker, invalidate the turn, cancel the server request by
its X-Request-ID, and re-check readiness before the next inference, because a
cancel acknowledgement does not mean the GPU is clean yet.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from app.config import Config
from app.hardware.camera import CameraError
from app.hardware.input import Action
from app.server.errors import AuthError, BMOError, RequestCancelled, ServerUnavailable
from app.ui.states import BotState

log = logging.getLogger(__name__)

BUSY = (BotState.CAPTURING, BotState.THINKING, BotState.SPEAKING)


class InteractionController:
    def __init__(self, cfg: Config, ui, client, reservation, microphone, speaker, camera=None):
        self.cfg = cfg
        self.ui = ui
        self.client = client
        self.reservation = reservation
        self.mic = microphone
        self.speaker = speaker
        self.camera = camera

        self._lock = threading.RLock()
        self._state = BotState.WARMUP
        self._generation = 0
        self._active_request_id: str | None = None
        self._needs_readiness = False
        self._vision_armed = False
        self._listen_timer: threading.Timer | None = None
        self._idle = threading.Event()
        self._closed = False
        self.last_transcript: str | None = None
        self.last_reply: str | None = None

        cfg.runtime_path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> BotState:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    def _set_state(self, state: BotState, message: str = "", gen: int | None = None) -> bool:
        """Apply a state change; a stale turn (gen != current) is ignored."""
        with self._lock:
            if gen is not None and gen != self._generation:
                return False
            self._state = state
            if state in (BotState.IDLE, BotState.ERROR):
                self._idle.set()
            else:
                self._idle.clear()
        log.info("state -> %s %s", state.value, message)
        self.ui.set_state(state, message)
        return True

    def _stale(self, gen: int) -> bool:
        return gen != self._generation

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    # ---------------------------------------------------------------- startup
    def start(self) -> None:
        """Warm up: hardware setup + server health. Does not reserve the GPU."""
        self._set_state(BotState.WARMUP, "Waking up...")
        try:
            self.mic.configure()
        except Exception as e:  # mixer problems never block startup
            log.warning("Microphone setup failed: %s", e)
        try:
            self.client.health()
        except BMOError as e:
            self._set_state(BotState.ERROR, f"Server unreachable: {e}")
            return
        self._set_state(BotState.IDLE, "Press Start to talk")
        if self.cfg.sounds.enabled and self.cfg.sounds.greeting:
            self.speaker.play_effect("greeting")

    # ---------------------------------------------------------------- actions
    def handle_action(self, action: Action) -> None:
        if self._closed:
            return
        state = self._state
        if action is Action.START:
            if state in (BotState.IDLE, BotState.ERROR):
                self._start_listening()
            elif state is BotState.LISTENING:
                self._finish_listening()
            elif state in BUSY:
                self.interrupt()
        elif action is Action.INTERRUPT:
            if state is BotState.LISTENING:
                self._abort_listening()
            elif state in BUSY:
                self.interrupt()
        elif action is Action.B:
            if self.cfg.camera.vision_mode == "manual":
                self._vision_armed = not self._vision_armed
                self.ui.show_text("Camera armed for next turn" if self._vision_armed
                                  else "Camera disarmed", "bmo")
        elif action is Action.QUIT:
            self.shutdown()

    def submit_text(self, text: str) -> None:
        text = text.strip()
        if not text or self._closed:
            return
        if self._state in BUSY:
            self.interrupt()
        elif self._state is BotState.LISTENING:
            self._abort_listening()
        with self._lock:
            gen = self._generation
        self._spawn(gen, text=text)

    # -------------------------------------------------------------- listening
    def _start_listening(self) -> None:
        with self._lock:
            gen = self._generation
        try:
            self.mic.start()
        except Exception as e:
            log.exception("Could not start recording")
            self._set_state(BotState.ERROR, f"Microphone error: {e}")
            return
        self._set_state(BotState.LISTENING, "Listening... press Start when done")
        timer = threading.Timer(self.cfg.microphone.max_seconds, self._listen_timeout, args=(gen,))
        timer.daemon = True
        self._listen_timer = timer
        timer.start()

    def _listen_timeout(self, gen: int) -> None:
        if not self._stale(gen) and self._state is BotState.LISTENING:
            log.info("Recording hit max_seconds; submitting")
            self._finish_listening()

    def _cancel_listen_timer(self) -> None:
        if self._listen_timer is not None:
            self._listen_timer.cancel()
            self._listen_timer = None

    def _finish_listening(self) -> None:
        self._cancel_listen_timer()
        with self._lock:
            gen = self._generation
        # Leave LISTENING right away so a double press can't stop twice.
        self._set_state(BotState.THINKING, "Processing audio...", gen)
        self._spawn(gen, record=True)

    def _abort_listening(self) -> None:
        self._cancel_listen_timer()
        try:
            self.mic.abort()
        except Exception as e:
            log.warning("Microphone abort failed: %s", e)
        self._set_state(BotState.IDLE, "Cancelled")

    # -------------------------------------------------------------- interrupt
    def interrupt(self) -> None:
        # 1. Stop physical playback immediately.
        try:
            self.speaker.stop()
        except Exception as e:
            log.warning("Speaker stop failed: %s", e)
        # 2. Invalidate the in-flight turn so its response is never replayed.
        with self._lock:
            self._generation += 1
            request_id, self._active_request_id = self._active_request_id, None
            if request_id is not None:
                self._needs_readiness = True
        # 3. Cancel the server request without blocking the caller.
        if request_id is not None:
            log.info("Interrupt: cancelling request %s", request_id)
            threading.Thread(target=self.client.cancel, args=(request_id,),
                             name="bmo-cancel", daemon=True).start()
        # 4. Readiness is re-checked by the next turn (see _run_turn).
        self._set_state(BotState.IDLE, "Interrupted")

    # ------------------------------------------------------------------ turns
    def _spawn(self, gen: int, *, record: bool = False, text: str | None = None) -> None:
        threading.Thread(target=self._run_turn, args=(gen,), kwargs={"record": record, "text": text},
                         name=f"bmo-turn-{gen}", daemon=True).start()

    def _want_image(self) -> bool:
        mode = self.cfg.camera.vision_mode
        if self.camera is None or not self.cfg.camera.enabled or mode == "off":
            return False
        if mode == "manual":
            armed, self._vision_armed = self._vision_armed, False
            return armed
        return True

    def _run_turn(self, gen: int, *, record: bool = False, text: str | None = None) -> None:
        try:
            audio = None
            if record:
                audio = self.mic.stop()
                if self._stale(gen):
                    return
                if audio is None:
                    self._set_state(BotState.IDLE, "I didn't hear anything", gen)
                    return

            image = None
            if self._want_image():
                self._set_state(BotState.CAPTURING, "Looking...", gen)
                try:
                    image = self.camera.capture()
                except CameraError as e:
                    log.warning("Camera capture failed, continuing without image: %s", e)
                if self._stale(gen):
                    return

            if not self._set_state(BotState.THINKING, "Thinking...", gen):
                return
            if self.cfg.sounds.enabled and self.cfg.sounds.ack:
                self.speaker.play_effect("ack")

            with self._lock:
                recheck, self._needs_readiness = self._needs_readiness, False
            if recheck:
                # The previous request was cancelled: wait for the server to report
                # bmo_ready again before sending replacement inference.
                self.reservation.mark_stale()
            self.reservation.ensure_ready()

            request_id = self.client.new_request_id()
            with self._lock:
                if self._stale(gen):
                    return
                self._active_request_id = request_id
            try:
                result = self.client.interact(text=text, audio_path=audio, image_path=image,
                                              speak=True, request_id=request_id)
            finally:
                with self._lock:
                    if self._active_request_id == request_id:
                        self._active_request_id = None
            if self._stale(gen):
                log.info("Dropping response for interrupted request %s", request_id)
                return
            self.reservation.renewed()

            self.last_transcript = result.transcript or text
            self.last_reply = result.text
            if self.last_transcript:
                self.ui.show_text(self.last_transcript, "user")
            self.ui.show_text(result.text, "bmo")

            if result.audio_wav:
                reply = Path(self.cfg.runtime_path) / f"reply-{gen}.wav"
                reply.write_bytes(result.audio_wav)
                if not self._set_state(BotState.SPEAKING, "", gen):
                    return
                self.speaker.play(reply, block=True)
            self._set_state(BotState.IDLE, "", gen)
        except RequestCancelled:
            log.info("Request cancelled")
            self._set_state(BotState.IDLE, "Cancelled", gen)
        except AuthError as e:
            log.error("Authentication failed: %s", e)
            self._set_state(BotState.ERROR, "Server rejected the BMO credential", gen)
        except ServerUnavailable as e:
            log.error("Server unavailable: %s", e)
            self._set_state(BotState.ERROR, "Can't reach the BMO server", gen)
        except Exception as e:
            log.exception("Interaction failed")
            self._set_state(BotState.ERROR, f"Something went wrong: {e}", gen)

    # --------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_listen_timer()
        if self._state is BotState.LISTENING:
            try:
                self.mic.abort()
            except Exception:
                pass
        if self._state in BUSY:
            self.interrupt()
        try:
            self.speaker.stop()
        except Exception:
            pass
        self.reservation.release()
        self._idle.set()
        log.info("Controller shut down")

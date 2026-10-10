"""Interaction state machine: record -> (camera) -> server -> speak, with interruption.

One interaction runs at a time on a worker thread. Every turn captures the
current ``generation``; an interrupt bumps it, so any late response from an
interrupted turn is dropped and never played. Interrupt order (per the server
contract): stop the speaker, invalidate the turn, cancel the server request by
its X-Request-ID, and re-check readiness before the next inference, because a
cancel acknowledgement does not mean the GPU is clean yet.

Event feed (dicts passed to subscribers, each also carries "time"). Schema:
  state      {state, message, turn, source}       UI state; turn/source may be None
  text       {who: user|bmo, text, turn}          transcript / reply text
  error      {code, message, hint}                error (also notice for soft codes)
  notice     {code, message, hint}
  phase      {phase, turn, source, client_id, message}
               phase: starting|idle|listening|looking|thinking|waiting_gpu|speaking|error
  audio      {turn, url: "/api/audio/<id>.wav", client_id, source, duration}
               emitted when a reply WAV is saved, before any Pi playback
  turn_done  {turn, source, client_id, ok, spoke_on_pi, goodbye, interrupted}   once per turn,
               also on interrupt; goodbye = the user said farewell: no follow-up, session ends;
               interrupted = the user cut BMO off mid-reply on the Pi: listen for a follow-up
  live       {armed, model, error, ...}           published by others via publish(); sticky
  setting    {key, value}                         after a live setting is applied
  status     {turn, status, text}                 BMO is working on something during a turn
               ("Searching memories..."); also sets the phase message
  memory     {turn, changes: [{op, name}]}        the server saved or forgot long-term memory
  session    {id, reason, consolidated}           a conversation session ended and a fresh one
               started (id = the NEW session). reason: new_session|idle|startup.
               consolidated: ok (saved to long-term memory) | pending (server unreachable,
               kept on disk and retried) | unavailable (long-term memory is off on the server) |
               skipped (memory off or nothing said). Clients should clear their chat view.
``history`` (replayed to new clients) holds only text and error events;
``snapshot()`` returns the sticky last phase and live events.
"""

from __future__ import annotations

import io
import logging
import threading
import time
import wave
import json
import queue
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.config import Config
from app.hardware.camera import CameraError
from app.hardware.input import Action
from app.memory import (ConversationMemory, ConversationSession, is_forget_command,
                        is_goodbye, is_new_session_command, write_json_atomic)
from app.replies import ReplyStore
from app.notify import ERRORS, NOTICES, SERVER_CODES, SOFT, clip_path
from app.server.errors import (AuthError, BadResponse, BMOError, RequestCancelled,
                               ReservationTimeout, ServerBusy, ServerUnavailable)
from app.ui.states import BotState

log = logging.getLogger(__name__)

BUSY = (BotState.CAPTURING, BotState.THINKING, BotState.SPEAKING)

LISTEN_SOURCES = ("button", "wake", "followup")
PHASES = {
    BotState.WARMUP: "starting", BotState.IDLE: "idle", BotState.LISTENING: "listening",
    BotState.CAPTURING: "looking", BotState.THINKING: "thinking",
    BotState.SPEAKING: "speaking", BotState.ERROR: "error",
}
HEALTH_BACKOFF = (5.0, 60.0)  # first retry delay, cap (seconds)
SESSION_POLL = 15.0            # how often the idle thread checks the session (seconds)
MAX_PENDING_SESSIONS = 20


@dataclass
class Turn:
    """Context of one interaction, from submit to turn_done."""
    id: str
    gen: int
    source: str
    client_id: str | None
    speak: bool
    play_on_pi: bool
    done: bool = False
    goodbye: bool = False
    played: int = 0                 # server actions already queued (streamed as they came)
    song: str | None = None         # mood, when BMO sings this reply
    music: bool = False             # a music track follows the speech in the reply audio
    music_audio: bytes | None = None   # the track itself, when the reply has no audio
    heard: str | None = None        # transcript already shown from the stream
    anims: "_Lane | None" = None    # expression and face actions, one after another
    sounds: "_Lane | None" = None   # sound actions, one after another, alongside the anims
    reply_stream: "_ReplyStream | None" = None   # reply audio arriving in pieces
    reply_shown: str | None = None  # reply text already shown from the stream
    faced: bool = False             # the reply's face (or singing screen) is queued
    sent_at: float = 0.0            # monotonic time the request went out
    cut_off: bool = False           # the user interrupted its reply on the Pi


class _Lane:
    """A playback queue with its own thread: items play one after another, in order, from
    the first put. Two lanes run side by side, so a sound plays while BMO dances. Once
    the turn is stale, the rest is skipped."""

    def __init__(self, name: str, play: Callable[[Any], None], alive: Callable[[], bool]):
        self._name, self._play, self._alive = name, play, alive
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._closed = self._dropped = False

    def put(self, item) -> None:
        self._q.put(item)
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()

    @property
    def busy(self) -> bool:
        return self._q.unfinished_tasks > 0

    def close(self) -> None:
        """The turn is over: skip whatever is still queued and let the thread end."""
        self._closed = self._dropped = True

    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=0.1)
            except queue.Empty:
                if self._closed or not self._alive():
                    return
                continue
            try:
                if self._alive() and not self._dropped:
                    self._play(item)
            except Exception:
                log.exception("%s: playback failed", self._name)
            finally:
                self._q.task_done()


class _ReplyStream:
    """Streamed reply audio (audio_stream): the pieces play in index order, back to back,
    through one open speaker stream, so a song cut on bar lines has no gaps. Playback
    starts with the first piece once the turn's sounds have played; pieces arriving
    meanwhile wait. A piece still missing when its turn comes is waited for, never
    skipped, unless the reply is complete without it."""

    def __init__(self, ctl: "InteractionController", turn: Turn, gen: int):
        self._ctl, self._turn, self._gen = ctl, turn, gen
        self._cond = threading.Condition()
        self._pieces: dict[int, bytes] = {}
        self._complete = False
        self._music_start: float | None = None
        self._music_thread = False
        self._started_at: float | None = None
        self._out = None
        self.ok = False
        self.done = threading.Event()
        threading.Thread(target=self._run, name=f"bmo-reply-{gen}", daemon=True).start()

    def add(self, index: int, wav: bytes) -> None:
        with self._cond:
            self._pieces[index] = wav
            self._cond.notify_all()

    def finish(self, music_start_s: float | None) -> None:
        """All pieces are in (the result came)."""
        with self._cond:
            self._complete = True
            self._music_start = music_start_s
            self._cond.notify_all()
        self._start_music()

    def wait(self) -> bool:
        """Block until the reply has played; False if interrupted or it failed."""
        while not self.done.wait(0.05):
            if self._ctl._stale(self._gen):
                return False
        return self.ok

    def abort(self) -> None:
        with self._cond:
            self._complete = True
            self._cond.notify_all()
        out = self._out
        if out is not None and not self.done.is_set():
            out.abort()

    @property
    def started(self) -> bool:
        return self._started_at is not None

    def _alive(self) -> bool:
        return not self._ctl._stale(self._gen)

    def _start_music(self) -> None:
        """At music_start_s (from the first piece) the track begins: the music screen."""
        with self._cond:
            if self._music_start is None or self._started_at is None or self._music_thread:
                return
            self._music_thread = True
            left = self._music_start - (time.monotonic() - self._started_at)
        threading.Thread(target=self._ctl._music_show, args=(max(0.0, left), self._gen, self.done),
                         name=f"bmo-music-{self._gen}", daemon=True).start()

    def _next(self, index: int) -> tuple[int, bytes] | None:
        """(index, piece) once that piece is here, or the next one when the reply is
        complete without it; None when the reply ends first (or the turn is stale)."""
        with self._cond:
            while index not in self._pieces:
                if not self._alive():
                    return None
                if self._complete:
                    later = [i for i in self._pieces if i > index]
                    if not later:
                        return None
                    log.warning("Reply audio piece %d never came; going on", index)
                    index = min(later)
                    break
                self._cond.wait(0.05)
            return index, self._pieces.pop(index)

    def _run(self) -> None:
        ctl, turn, gen = self._ctl, self._turn, self._gen
        try:
            self._play(ctl, turn, gen)
        except Exception:
            log.exception("Streamed reply playback failed")
        finally:
            out, self._out = self._out, None
            if out is not None:
                if self._alive():
                    self.ok = out.close() and self.ok
                else:
                    out.abort()
            self.done.set()

    def _play(self, ctl: "InteractionController", turn: Turn, gen: int) -> None:
        if not ctl._drain(turn.sounds, gen):
            return
        got = self._next(0)
        if got is None:
            return
        index, first = got
        try:
            with wave.open(io.BytesIO(first)) as w:
                fmt = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                pcm = w.readframes(w.getnframes())
        except (wave.Error, EOFError):
            log.error("Reply audio: first piece is not a WAV")
            return
        if fmt[1] != 2:
            log.error("Reply audio: %d-byte samples, expected 16-bit", fmt[1])
            return
        prepare = getattr(ctl.ui, "prepare_speech", None)
        if prepare is not None:
            prepare(first)              # lets the face follow the reply's loudness
        time_lyrics = getattr(ctl.ui, "time_lyrics", None) if turn.song else None
        if time_lyrics is not None:
            time_lyrics()               # each line shows when its piece starts playing
        if not ctl._set_state(BotState.SPEAKING, "", gen):
            return
        self._out = ctl.speaker.open_stream(fmt[2], fmt[0])
        if self._out is None:
            return
        log.info("Reply audio starts (audio=stream), %.2f s after the request",
                 time.monotonic() - turn.sent_at)
        with self._cond:
            self._started_at = time.monotonic()
        self._start_music()
        append = getattr(ctl.ui, "append_speech", None)
        lyric_start = getattr(ctl.ui, "add_lyric_start", None) if turn.song else None
        offset = 0.0
        while True:
            if pcm and not self._out.write(pcm):
                return              # stopped (button, wake word or a new sound)
            offset += len(pcm) / (2 * fmt[0] * fmt[2])
            got = self._next(index + 1)
            if got is None:
                break
            index, piece = got
            pcm = self._pcm(piece, fmt, index)
            if not pcm:
                continue
            if append is not None:
                append(piece)
            if lyric_start is not None:
                lyric_start(offset)     # a song: one piece per line after the opening bar
        self.ok = self._alive()

    @staticmethod
    def _pcm(piece: bytes, fmt: tuple, index: int) -> bytes:
        try:
            with wave.open(io.BytesIO(piece)) as w:
                if (w.getnchannels(), w.getsampwidth(), w.getframerate()) == fmt:
                    return w.readframes(w.getnframes())
        except (wave.Error, EOFError):
            pass
        log.warning("Reply audio piece %d is unreadable or in another format; skipped", index)
        return b""


class _Stale(Exception):
    """The turn was interrupted while its reply was still streaming."""


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
        # Event feed for the web page: dicts with "type" in state/text/error.
        self._subscribers: list[Callable[[dict], None]] = []
        self.history: deque[dict] = deque(maxlen=50)  # recent text/error events
        self._status_cache: tuple[float, dict] | None = None
        self._waiting_announced = False
        self._turn: Turn | None = None
        self.listening_source: str | None = None
        self.idle_hint = "Press Start to talk"
        self._sticky: dict[str, dict] = {}
        self._health_thread: threading.Thread | None = None
        self._health_stop = threading.Event()
        self.health_backoff = HEALTH_BACKOFF   # tests may shorten it
        self._prewarming = False
        # Say "waiting for my brain server" while the reservation polls bmo_ready.
        reservation.on_wait = self._on_server_wait

        cfg.runtime_path.mkdir(parents=True, exist_ok=True)
        self.memory = ConversationMemory(cfg.runtime_path / cfg.memory.file,
                                         cfg.memory.max_messages, cfg.memory.max_chars)
        self._memory_epoch = 0  # bumped by every clear; a turn started before one is not stored
        self.session = ConversationSession(cfg.runtime_path / "session.json")
        # The server logs memory-on turns under X-Session-ID and consolidates them when the
        # session ends, so the client always sends this session's id.
        self.client.session_id = self.session.id
        self.pending_dir = cfg.runtime_path / "pending_sessions"
        self.session_poll = SESSION_POLL        # tests may shorten it
        self._session_lock = threading.Lock()   # snapshot + reset of the session
        self._pending_lock = threading.Lock()   # pending_sessions/ files
        self._session_thread: threading.Thread | None = None
        self._session_stop = threading.Event()
        ReplyStore.purge_legacy(cfg.runtime_path)
        self.replies = ReplyStore(cfg.runtime_path / "replies", keep=8)
        # The last few Pi mic recordings, by turn id, to check what Whisper heard.
        self.heard = ReplyStore(cfg.runtime_path / "heard", keep=10)

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
        turn = None if state in (BotState.WARMUP, BotState.LISTENING) else self._turn
        turn_id, source, client_id = self._turn_fields(turn, state)
        self.ui.set_state(state, message)
        self._emit({"type": "state", "state": state.value, "message": message,
                    "turn": turn_id, "source": source})
        self._emit({"type": "phase", "phase": PHASES[state], "turn": turn_id,
                    "source": source, "client_id": client_id, "message": message})
        return True

    def _turn_fields(self, turn: Turn | None, state: BotState | None = None):
        if turn is not None:
            return turn.id, turn.source, turn.client_id
        source = self.listening_source if state is BotState.LISTENING else None
        return None, source, None

    # ----------------------------------------------------------------- events
    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        """Receive every event dict; returns an unsubscribe function."""
        with self._lock:
            self._subscribers.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)
        return unsubscribe

    def publish(self, event: dict) -> None:
        """Emit an event to every subscriber (live events from other components too)."""
        self._emit(event)

    def snapshot(self) -> list[dict]:
        """Sticky events a new client needs: the last phase, then the last live."""
        with self._lock:
            return [self._sticky[t] for t in ("phase", "live") if t in self._sticky]

    def _emit(self, event: dict) -> None:
        event = {"time": time.time(), **event}
        if event["type"] in ("text", "error"):
            self.history.append(event)
        if event["type"] in ("phase", "live"):
            with self._lock:
                self._sticky[event["type"]] = event
        with self._lock:
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(event)
            except Exception:
                log.exception("Event subscriber failed")

    def _show(self, text: str, who: str = "bmo") -> None:
        self.ui.show_text(text, who)
        turn = self._turn
        self._emit({"type": "text", "who": who, "text": text,
                    "turn": turn.id if turn else None})

    # ------------------------------------------------------------ notifications
    def notify(self, code: str, *, gen: int | None = None, server_message: str = "",
               play: bool = True) -> bool:
        """Report an error/notice: state, console/screen text, web event, and the
        pre-recorded clip unless muted. Returns False if the turn went stale."""
        info = ERRORS.get(code) or ERRORS["unknown"]
        message = server_message or info.message
        if code not in NOTICES:
            state = BotState.IDLE if code in SOFT else BotState.ERROR
            if not self._set_state(state, message, gen):
                return False
        elif gen is not None and self._stale(gen):
            return False
        log.info("notify %s: %s", code, message)
        self.ui.show_text(f"{message} {info.hint}", "bmo")
        self._emit({"type": "error" if code not in NOTICES else "notice",
                    "code": code, "message": message, "hint": info.hint})
        if play and not self.cfg.ui.text_only:
            clip = clip_path(self.speaker.sounds_dir, code) if hasattr(self.speaker, "sounds_dir") else None
            if clip is not None and clip.is_file():
                self.speaker.play(clip, block=False)
        return True

    def _on_server_wait(self, status: dict) -> None:
        """Reservation is polling for bmo_ready: tell the user once per turn."""
        if self._waiting_announced or self._state is not BotState.THINKING:
            return
        self._waiting_announced = True
        with self._lock:
            gen = self._generation
        log.info("Waiting for BMO workers (server state %s, queued %s)",
                 status.get("scheduling_state"), status.get("queued_requests"))
        turn = self._turn
        self.notify("server_waiting", gen=gen)
        turn_id, source, client_id = self._turn_fields(turn)
        self._emit({"type": "phase", "phase": "waiting_gpu", "turn": turn_id, "source": source,
                    "client_id": client_id, "message": ERRORS["server_waiting"].message})

    # --------------------------------------------------------------- settings
    def apply_setting(self, key: str, value: Any) -> Any:
        """Validate, apply live and persist one user setting (see app/settings.py)."""
        from app.settings import save_settings, set_setting
        value = set_setting(self.cfg, key, value)
        if key == "speaker.volume":
            try:
                self.speaker.configure()
            except Exception as e:
                log.warning("Volume change failed: %s", e)
        elif key == "camera.vision_mode":
            self._vision_armed = False
        elif key == "ui.text_only" and value:
            self.speaker.stop()
        save_settings(self.cfg)
        self.publish({"type": "setting", "key": key, "value": value})
        return value

    def server_status(self, max_age: float = 5.0) -> dict:
        """Small /v1/status summary for display, cached for max_age seconds."""
        now = time.monotonic()
        cached = self._status_cache
        if cached is not None and now - cached[0] < max_age:
            return cached[1]
        try:
            raw = self.client.status()
            summary = {"reachable": True, "bmo_ready": bool(raw.get("bmo_ready")),
                       "mode": raw.get("scheduling_state") or raw.get("state") or raw.get("mode"),
                       "active_requests": raw.get("active_requests"),
                       "queued_requests": raw.get("queued_requests"),
                       "last_error": raw.get("last_error") or raw.get("failure_reason")}
        except BMOError as e:
            summary = {"reachable": False, "error": type(e).__name__}
        self._status_cache = (now, summary)
        return summary

    def _stale(self, gen: int) -> bool:
        return gen != self._generation

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    # ---------------------------------------------------------------- startup
    def start(self) -> None:
        """Warm up: hardware setup + server health. Does not reserve the GPU."""
        self._set_state(BotState.WARMUP, "Waking up...")
        self._start_session_thread()
        self._startup_session()
        try:
            self.mic.configure()
        except Exception as e:  # mixer problems never block startup
            log.warning("Microphone setup failed: %s", e)
        try:
            self.speaker.configure()
        except Exception as e:
            log.warning("Speaker setup failed: %s", e)
        try:
            self.client.health()
        except BMOError as e:
            log.warning("Server health check failed: %s", e)
            self.notify("server_unreachable")
            self._start_health_retry(startup=True)
            return
        self._set_state(BotState.IDLE, self.idle_hint)
        self._greet()

    # --------------------------------------------------------------- sessions
    def _start_session_thread(self) -> None:
        with self._lock:
            if self._closed or (self._session_thread is not None and self._session_thread.is_alive()):
                return
            t = threading.Thread(target=self._session_loop, name="bmo-session-idle", daemon=True)
            self._session_thread = t
        t.start()

    def _session_loop(self) -> None:
        while not self._session_stop.wait(self.session_poll):
            try:
                self._idle_tick()
            except Exception:
                log.exception("Session idle check failed")

    def _idle_limit(self) -> float:
        return float(self.cfg.memory.session_idle_minutes) * 60

    def _idle_tick(self) -> bool:
        """End the session if it has been quiet for session_idle_minutes. True if it ended."""
        limit = self._idle_limit()
        if limit <= 0 or self._closed or self.session.is_empty:
            return False
        if self._state not in (BotState.IDLE, BotState.ERROR) or self._turn is not None:
            return False
        if self.session.idle_seconds() < limit:
            return False
        self.end_session("idle")
        return True

    def _startup_session(self) -> None:
        """A leftover session from before a restart is consolidated once it is stale."""
        limit = self._idle_limit()
        if limit > 0 and not self.session.is_empty and self.session.idle_seconds() >= limit:
            threading.Thread(target=self.end_session, args=("startup",),
                             name="bmo-consolidate", daemon=True).start()

    def session_info(self) -> dict:
        return {"id": self.session.id, "started_at": self.session.started_at,
                "messages": len(self.session)}

    def new_session(self) -> dict:
        """Interrupt whatever is going on, save the conversation and start a fresh one."""
        self._preempt()
        return self.end_session("new_session")

    def end_session(self, reason: str) -> dict:
        """Consolidate the finished session into long-term memory, then start a new one.
        Always starts a fresh session (clearing short-term memory and the chat history).
        Never raises."""
        out: dict = {"id": None, "reason": reason, "consolidated": "skipped"}
        try:
            with self._session_lock:
                snap = self.session.to_dict()
                snap["ended_at"] = self.session._clock()
                out["id"] = self._reset_session()
                self._clear_memory()
            self.history.clear()
            if self.cfg.memory.enabled and snap["messages"]:
                out["consolidated"] = self._consolidate(snap, reason)
            if self.cfg.memory.enabled:
                self._retry_pending()
        except Exception:
            log.exception("Ending session failed")
        self.publish({"type": "session", "id": out["id"] or self.session.id,
                      "reason": reason, "consolidated": out["consolidated"]})
        return out

    def _reset_session(self) -> str:
        """Start a new session (caller holds _session_lock); later turns are logged under it."""
        sid = self.session.reset()
        self.client.session_id = sid
        return sid

    def _consolidate(self, snap: dict, reason: str) -> str:
        """Ask the server to end the session; it consolidates the turns it logged."""
        payload = {"session_id": snap["id"], "reason": reason}
        try:
            res = self.client.end_session(snap["id"])
        except Exception as e:
            log.warning("Consolidating session %s failed, keeping it for later: %s", snap["id"], e)
            self._save_pending(payload)
            return "pending"
        return "unavailable" if res is None else "ok"

    def _save_pending(self, payload: dict) -> None:
        with self._pending_lock:
            write_json_atomic(self.pending_dir / f"{payload['session_id']}.json", payload,
                              ".pending-")
            files = self._pending_files()
            for old in files[:-MAX_PENDING_SESSIONS]:
                old.unlink(missing_ok=True)

    def _pending_files(self) -> list[Path]:
        try:
            files = [f for f in self.pending_dir.glob("*.json") if f.is_file()]
            return sorted(files, key=lambda f: (f.stat().st_mtime, f.name))
        except OSError:
            return []

    def _retry_pending(self) -> int:
        """Send saved sessions to the server, oldest first; stops at the first failure."""
        sent = 0
        with self._pending_lock:
            for f in self._pending_files():
                try:
                    payload = json.loads(f.read_text())
                    res = self.client.end_session(str(payload["session_id"]))
                except (OSError, ValueError, TypeError, KeyError):
                    f.unlink(missing_ok=True)      # unreadable: nothing to retry
                    continue
                except Exception as e:
                    log.info("Pending session retry failed: %s", e)
                    break
                if res is None:
                    break                          # memory is off on the server; keep them
                f.unlink(missing_ok=True)
                sent += 1
        return sent

    def _discard_pending(self) -> None:
        with self._pending_lock:
            for f in self._pending_files():
                f.unlink(missing_ok=True)

    def _greet(self) -> None:
        if not self.cfg.ui.text_only and self.cfg.sounds.enabled and self.cfg.sounds.greeting:
            self.speaker.play_effect("greeting")

    # ----------------------------------------------------------- health retry
    def _start_health_retry(self, startup: bool = False) -> None:
        """One daemon thread polls /health with backoff until the server answers."""
        with self._lock:
            if self._closed or (self._health_thread is not None and self._health_thread.is_alive()):
                return
            t = threading.Thread(target=self._health_loop, args=(startup,),
                                 name="bmo-health", daemon=True)
            self._health_thread = t
        t.start()

    def _health_loop(self, startup: bool) -> None:
        delay, cap = self.health_backoff
        while not self._closed:
            if self._health_stop.wait(delay):
                return
            try:
                self.client.health()
            except Exception as e:
                log.info("Server still unreachable: %s", e)
                delay = min(delay * 2, cap)
                continue
            if self.cfg.memory.enabled:
                self._retry_pending()
            if self._state is not BotState.ERROR:
                return                      # something else already recovered
            if self._turn is not None:
                continue                    # a turn is running; look again shortly
            self._set_state(BotState.IDLE, "BMO is ready")
            if startup:
                self._greet()
            return

    # ---------------------------------------------------------------- actions
    def handle_action(self, action: Action) -> None:
        if self._closed:
            return
        state = self._state
        if action is Action.START:
            if state in (BotState.IDLE, BotState.ERROR):
                self.start_listening("button")
            elif state is BotState.LISTENING:
                self.finish_listening()
            elif state in BUSY:
                self.interrupt()
        elif action is Action.INTERRUPT:
            if state is BotState.LISTENING:
                self.cancel_listening()
            elif state in BUSY:
                self.interrupt()
        elif action is Action.B:
            if self.cfg.camera.vision_mode == "manual":
                self._vision_armed = not self._vision_armed
                self._show("Camera armed for next turn" if self._vision_armed
                           else "Camera disarmed", "bmo")
        elif action is Action.QUIT:
            self.shutdown()

    def _new_turn(self, speak: bool | None, play_on_pi: bool | None, source: str,
                  client_id: str | None) -> Turn:
        if speak is None:
            speak = not self.cfg.ui.text_only
        if play_on_pi is None:
            play_on_pi = speak
        with self._lock:
            gen = self._generation
        return Turn(self.replies.new_id(), gen, source, client_id, bool(speak), bool(play_on_pi))

    def _preempt(self) -> None:
        if self._state in BUSY:
            self.interrupt(followup=False)
        elif self._state is BotState.LISTENING:
            self.cancel_listening()

    def submit_text(self, text: str, speak: bool | None = None, *, play_on_pi: bool | None = None,
                    source: str = "web", client_id: str | None = None) -> str | None:
        """Send typed text. speak=None follows the mute setting (ui.text_only);
        play_on_pi=None follows speak. Returns the turn id, or None if nothing was sent."""
        text = (text or "").strip()
        if not text or self._closed:
            return None
        self._preempt()
        if is_forget_command(text):
            self._show(text, "user")
            threading.Thread(target=self.forget_memory, name="bmo-forget", daemon=True).start()
            self._show("Okay! BMO forgot everything.", "bmo")
            return None
        if is_new_session_command(text):
            self._show(text, "user")
            threading.Thread(target=self.new_session, name="bmo-session", daemon=True).start()
            self._show("Okay! Fresh start.", "bmo")
            return None
        turn = self._new_turn(speak, play_on_pi, source, client_id)
        self._spawn(turn, text=text)
        return turn.id

    def submit_audio(self, path, *, speak: bool | None = None, play_on_pi: bool | None = None,
                     source: str = "web", client_id: str | None = None) -> str | None:
        """Send a recorded clip (e.g. from the web page). Takes ownership of `path`:
        it is deleted when the turn ends."""
        if self._closed:
            self._delete(path)
            return None
        self._preempt()
        turn = self._new_turn(speak, play_on_pi, source, client_id)
        self._spawn(turn, audio=Path(path), own_audio=True)
        return turn.id

    @staticmethod
    def _delete(path) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            log.warning("Could not delete %s: %s", path, e)

    # ----------------------------------------------------------------- memory
    def _clear_memory(self) -> None:
        with self._lock:
            self._memory_epoch += 1
        self.memory.clear()

    def forget_memory(self) -> dict:
        """Wipe short-term memory here and long-term memory on the server. Never raises."""
        out: dict = {"local_cleared": True, "server": "ok", "forgotten": None}
        try:
            self._clear_memory()
            with self._session_lock:
                self._reset_session()
            self._discard_pending()
        except Exception:
            log.exception("Clearing local memory failed")
        try:
            n = self.client.forget_memories()
            if n is None:
                out["server"] = "unavailable"
            else:
                out["forgotten"] = n
        except Exception as e:
            log.warning("Server forget failed: %s", e)
            out["server"] = "error"
        return out

    def list_memories(self) -> dict:
        try:
            memories = self.client.list_memories()
        except Exception as e:
            log.warning("Listing memories failed: %s", e)
            return {"available": False, "memories": [], "error": type(e).__name__}
        if memories is None:
            return {"available": False, "memories": [], "error": None}
        return {"available": True, "memories": memories, "error": None}

    def delete_memory(self, name: str | int) -> dict:
        try:
            ok = self.client.delete_memory(name)
        except Exception as e:
            log.warning("Deleting memory %s failed: %s", name, e)
            return {"available": True, "deleted": False}
        if ok is None:
            return {"available": False, "deleted": False}
        return {"available": True, "deleted": bool(ok)}

    def save_memory(self, name: str, type: str, content: str) -> dict:
        """Create or edit one long-term memory topic on the server."""
        try:
            topic = self.client.put_memory(name, type, content)
        except BadResponse as e:
            return {"available": True, "saved": False,
                    "error": e.detail if e.status == 422 and e.detail else "Could not save"}
        except Exception as e:
            log.warning("Saving memory %s failed: %s", name, e)
            return {"available": True, "saved": False, "error": "Could not reach the server"}
        if topic is None:
            return {"available": False, "saved": False, "error": None}
        return {"available": True, "saved": True, "error": None, "memory": topic}

    def conversation(self) -> list[dict]:
        return self.memory.messages()

    def _remember(self, result, text: str | None, use_memory: bool, epoch: int) -> None:
        raw = getattr(result, "raw", None) or {}
        if raw.get("memory_reset"):
            self._clear_memory()
            return
        question = result.transcript or text
        with self._lock:
            unchanged = epoch == self._memory_epoch
        if use_memory and unchanged and question and result.text:
            self.memory.add_exchange(question, result.text)
            self.session.add_exchange(question, result.text)

    # -------------------------------------------------------------- listening
    def start_listening(self, source: str = "button") -> bool:
        """Start recording on the Pi mic. Only from IDLE/ERROR; source is button|wake|followup."""
        if source not in LISTEN_SOURCES or self._closed:
            return False
        self.session.touch()
        with self._lock:
            if self._state not in (BotState.IDLE, BotState.ERROR):
                return False
            gen = self._generation
            self.listening_source = source
        try:
            self.mic.start()
        except Exception:
            log.exception("Could not start recording")
            self.listening_source = None
            self.notify("mic_failed")
            return False
        message = "Listening... press Start when done" if source == "button" else "Listening..."
        self._set_state(BotState.LISTENING, message)
        timer = threading.Timer(self.cfg.microphone.max_seconds, self._listen_timeout, args=(gen,))
        timer.daemon = True
        self._listen_timer = timer
        timer.start()
        return True

    def _listen_timeout(self, gen: int) -> None:
        if not self._stale(gen) and self._state is BotState.LISTENING:
            log.info("Recording hit max_seconds; submitting")
            self.finish_listening()

    def _cancel_listen_timer(self) -> None:
        if self._listen_timer is not None:
            self._listen_timer.cancel()
            self._listen_timer = None

    def finish_listening(self) -> None:
        """Stop recording and send the clip as a turn (no-op unless LISTENING)."""
        with self._lock:
            if self._state is not BotState.LISTENING:
                return
            self._cancel_listen_timer()
            source = self.listening_source or "button"
            speak = not self.cfg.ui.text_only
            turn = Turn(self.replies.new_id(), self._generation, source, None, speak, speak)
            self.listening_source = None
            self._turn = turn
        # Leave LISTENING right away so a double press can't stop twice.
        self._set_state(BotState.THINKING, "Processing audio...", turn.gen)
        self._spawn(turn, record=True)

    def cancel_listening(self, quiet: bool = False) -> None:
        """Abort recording. quiet = straight back to IDLE with no "Cancelled" message."""
        with self._lock:
            if self._state is not BotState.LISTENING:
                return
            self._cancel_listen_timer()
            self.listening_source = None
        try:
            self.mic.abort()
        except Exception as e:
            log.warning("Microphone abort failed: %s", e)
        self._set_state(BotState.IDLE, self.idle_hint if quiet else "Cancelled")

    def prewarm(self) -> None:
        """Reserve the GPU in the background (e.g. on wake word). Single-flight, never raises."""
        with self._lock:
            if self._prewarming or self._closed:
                return
            self._prewarming = True
        threading.Thread(target=self._prewarm_run, name="bmo-prewarm", daemon=True).start()

    def _prewarm_run(self) -> None:
        try:
            self.reservation.ensure_ready()
        except Exception as e:
            log.info("Prewarm failed: %s", e)
        finally:
            self._prewarming = False

    def reply_path(self, turn_id: str) -> Path | None:
        return self.replies.path(turn_id)

    # -------------------------------------------------------------- interrupt
    def interrupt(self, followup: bool = True) -> None:
        """Stop the current turn. followup: if BMO was speaking on the Pi, the turn_done says
        so (interrupted), and the live listener opens a follow-up window to hear the user right
        away; the wake word alone was missed for many seconds after a cut-off reply."""
        if self._state is BotState.LISTENING:
            self.cancel_listening()
            return
        turn = self._turn
        if turn is not None and followup and self._state is BotState.SPEAKING:
            # Before the speaker stops: that lets the turn thread finish and emit turn_done.
            turn.cut_off = True
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
        clear_face = getattr(self.ui, "clear_face", None)
        if clear_face is not None:
            clear_face()
        turn = self._turn
        if turn is not None:
            self._finish_turn(turn, ok=False, spoke=False)

    # ------------------------------------------------------------------ turns
    def _spawn(self, turn: Turn, *, record: bool = False, text: str | None = None,
               audio: Path | None = None, own_audio: bool = False) -> None:
        with self._lock:
            self._turn = turn
        threading.Thread(target=self._run_turn, args=(turn,),
                         kwargs={"record": record, "text": text, "audio": audio,
                                 "own_audio": own_audio},
                         name=f"bmo-turn-{turn.gen}", daemon=True).start()

    def _finish_turn(self, turn: Turn, ok: bool, spoke: bool) -> None:
        """Emit turn_done exactly once per turn and release it."""
        with self._lock:
            if turn.done:
                return
            turn.done = True
            if self._turn is turn:
                self._turn = None
        cut_off = turn.cut_off and turn.play_on_pi
        self._emit({"type": "turn_done", "turn": turn.id, "source": turn.source,
                    "client_id": turn.client_id, "ok": ok, "spoke_on_pi": spoke or cut_off,
                    "goodbye": turn.goodbye, "interrupted": cut_off})
        if turn.goodbye and ok:
            # Conversation over: save it to long-term memory and start fresh for next time.
            threading.Thread(target=self.end_session, args=("goodbye",),
                             name="bmo-session", daemon=True).start()

    def _wait(self, seconds, gen: int, stop: threading.Event | None = None) -> bool:
        """Sleep while an animation plays; False as soon as the turn is interrupted
        (or stop is set)."""
        end = time.monotonic() + (seconds if isinstance(seconds, (int, float)) else 0.0)
        while (left := end - time.monotonic()) > 0:
            if self._stale(gen) or (stop is not None and stop.is_set()):
                return False
            time.sleep(min(0.05, left))
        return not self._stale(gen)

    def _drain(self, lane: "_Lane | None", gen: int) -> bool:
        """Wait until a playback lane is empty; False if the turn is interrupted first."""
        while lane is not None and lane.busy:
            if self._stale(gen):
                return False
            time.sleep(0.02)
        return not self._stale(gen)

    def _queue_action(self, action, turn: Turn, gen: int) -> None:
        """Hand one of BMO's actions to its lane, which starts playing it at once:
        expressions and faces to the animation lane, sounds to the sound lane, so both
        run side by side. Music is only noted (its track is in the reply audio).
        Unknown action types are ignored."""
        index, turn.played = turn.played, turn.played + 1
        if action.type in ("expression", "face"):
            self._lane(turn, gen, "anims").put(action)
        elif action.type == "sound":
            if turn.play_on_pi and not self.cfg.ui.text_only:
                self._lane(turn, gen, "sounds").put((index, action))
        elif action.type == "music":
            turn.music = True
            turn.music_audio = action.audio_wav or turn.music_audio

    def _lane(self, turn: Turn, gen: int, kind: str) -> "_Lane":
        lane = getattr(turn, kind)
        if lane is None:
            play = self._play_anim if kind == "anims" else self._play_sound
            lane = _Lane(f"bmo-{kind}-{gen}", lambda item: play(item, gen),
                         lambda: not self._stale(gen))
            setattr(turn, kind, lane)
        return lane

    def _play_anim(self, item, gen: int) -> None:
        """An expression or face action, held for its length; or a callable to run once
        the animations before it are done."""
        if callable(item):
            item()
            return
        ui_fn = getattr(self.ui, "play_expression" if item.type == "expression"
                        else "show_face", None)
        log.info("Animation: %s %s", item.type, item.name)
        self._wait(ui_fn(item.name) if ui_fn is not None else 0.0, gen)

    def _play_sound(self, item, gen: int) -> None:
        index, action = item
        log.info("Sound: %s", action.name)
        self.speaker.play_bytes(action.audio_wav,
                                self.cfg.runtime_path / "sfx" / f"sfx-{index}.wav", block=True)

    def _music_show(self, start_s: float, gen: int, done: threading.Event) -> None:
        """At start_s into the reply audio the music track begins: switch to the music
        screen and loop the dance until the audio ends (done) or the turn is interrupted."""
        if not self._wait(start_s, gen, done) or done.is_set():
            return
        log.info("Music track starts: music screen, looping dance")
        self._overlay(None, True, "")
        play = getattr(self.ui, "play_expression", None)
        while not done.is_set() and not self._stale(gen):
            seconds = play("dance") if play is not None else 0.0
            if not seconds:
                done.wait()     # no dance on this face set: the music screen alone
                return
            self._wait(seconds, gen, done)

    def _speak(self, path: Path, gen: int, music_start_s: float | None) -> None:
        """Play reply audio on the Pi to its end; with music_start_s, the music screen and a
        looping dance from that second on."""
        done = threading.Event()
        if music_start_s is not None:
            threading.Thread(target=self._music_show, args=(music_start_s, gen, done),
                             name=f"bmo-music-{gen}", daemon=True).start()
        log.info("Reply audio starts")
        try:
            self.speaker.play(path, block=True)
        finally:
            done.set()

    def _stream_handler(self, turn: Turn, gen: int) -> Callable[[dict], None]:
        """on_event for a streamed reply: runs on the turn thread while the reply arrives."""
        def on_event(ev: dict) -> None:
            if self._stale(gen):
                raise _Stale()
            kind = ev.get("type")
            if kind == "transcript" and isinstance(ev.get("text"), str) and ev["text"].strip():
                turn.heard = ev["text"].strip()
                self._show(turn.heard, "user")
            elif kind == "status":
                text = str(ev.get("text") or "Thinking...")
                self._set_state(BotState.THINKING, text, gen)
                caption = getattr(self.ui, "set_caption", None)
                if caption is not None:
                    caption(text)
                play = getattr(self.ui, "play_expression", None)
                if play is not None:
                    play(str(ev.get("expression") or "look_around"))
                self._emit({"type": "status", "turn": turn.id, "status": ev.get("status"),
                            "text": text})
            elif kind == "song":
                turn.song = str(ev.get("mood") or "happy")
            elif kind == "action":
                self._queue_action(ev["action"], turn, gen)
            elif kind == "reply":
                turn.song = ev.get("song") or turn.song
                text = str(ev.get("text") or "")
                if text:
                    turn.reply_shown = text
                    self._show(text, "bmo")
                self._reply_face(turn, ev.get("emotion") or "neutral", turn.song, text)
            elif kind == "audio_chunk":
                if turn.play_on_pi and not self.cfg.ui.text_only:
                    if turn.reply_stream is None:
                        turn.reply_stream = _ReplyStream(self, turn, gen)
                    turn.reply_stream.add(ev["index"], ev["audio"])
        return on_event

    def _reply_face(self, turn: Turn, emotion: str, song: str | None, text: str) -> None:
        """The reply's face (or the singing screen), once the face show's animations are
        done; the animations left keep playing over the speech."""
        if turn.faced:
            return
        turn.faced = True

        def show():
            set_emotion = getattr(self.ui, "set_emotion", None)
            if set_emotion is not None:
                set_emotion(emotion)    # after a face show, back to the reply's face
            if song:
                self._overlay(song, False, text)

        if turn.anims is not None and turn.anims.busy:
            turn.anims.put(show)
        else:
            show()

    def _overlay(self, song: str | None, music: bool, text: str) -> None:
        """Singing screen (notes + lyrics) or music screen while the reply plays."""
        set_overlay = getattr(self.ui, "set_overlay", None)
        if set_overlay is None:
            return
        if song:
            set_overlay("song", text.splitlines())
        elif music:
            set_overlay("music")

    @staticmethod
    def _wav_duration(data: bytes) -> float | None:
        try:
            with wave.open(io.BytesIO(data)) as w:
                return round(w.getnframes() / w.getframerate(), 3)
        except (wave.Error, EOFError, ZeroDivisionError):
            return None

    def _want_image(self) -> bool:
        mode = self.cfg.camera.vision_mode
        if self.camera is None or not self.cfg.camera.enabled or mode == "off":
            return False
        if mode == "manual":
            armed, self._vision_armed = self._vision_armed, False
            return armed
        return True

    def _run_turn(self, turn: Turn, *, record: bool = False, text: str | None = None,
                  audio: Path | None = None, own_audio: bool = False) -> None:
        gen, speak = turn.gen, turn.speak
        with self._lock:
            epoch = self._memory_epoch
        use_memory = self.cfg.memory.enabled
        self.session.touch()
        ok = spoke = False
        try:
            if record:
                audio = self.mic.stop()
                if self._stale(gen):
                    return
                if audio is None:
                    self.notify("nothing_heard", gen=gen)
                    return
                try:
                    self.heard.save(turn.id, Path(audio).read_bytes())
                except OSError as e:
                    log.warning("Could not keep the recording: %s", e)

            image = None
            if self._want_image():
                self._set_state(BotState.CAPTURING, "Looking...", gen)
                try:
                    image = self.camera.capture()
                except CameraError as e:
                    log.warning("Camera capture failed, continuing without image: %s", e)
                    self.notify("camera_failed", gen=gen, play=False)
                if self._stale(gen):
                    return

            if not self._set_state(BotState.THINKING, "Thinking...", gen):
                return
            if speak and turn.play_on_pi and self.cfg.sounds.enabled and self.cfg.sounds.ack:
                self.speaker.play_effect("ack")

            self._waiting_announced = False
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
                extra = ({"history": self.memory.messages(), "memory": True}
                         if use_memory else {"memory": False})
                if (self.cfg.audio_stream and speak and turn.play_on_pi
                        and not self.cfg.ui.text_only):
                    extra["audio_stream"] = True    # play the reply as it is made
                turn.sent_at = time.monotonic()
                result = self.client.interact(text=text, audio_path=audio, image_path=image,
                                              speak=speak, request_id=request_id,
                                              on_event=self._stream_handler(turn, gen), **extra)
            finally:
                with self._lock:
                    if self._active_request_id == request_id:
                        self._active_request_id = None
            if self._stale(gen):
                log.info("Dropping response for interrupted request %s", request_id)
                return
            self.reservation.renewed()
            raw = getattr(result, "raw", None) or {}
            if speak:
                log.info("Turn audio: asked=%s got=%s",
                         "stream" if extra.get("audio_stream") else "whole",
                         "stream" if getattr(result, "audio_streamed", False) else "whole")
            end_session = bool(raw.get("session_reset")) or (
                bool(result.transcript) and is_new_session_command(result.transcript))
            turn.goodbye = not end_session and is_goodbye(result.transcript or text or "")
            if not end_session:
                self._remember(result, text, use_memory, epoch)

            if result.transcript:
                log.info("Heard (%s, turn %s): %r", turn.source, turn.id, result.transcript)
            self.last_transcript = result.transcript or text
            self.last_reply = result.text
            if self.last_transcript and self.last_transcript != turn.heard:
                self._show(self.last_transcript, "user")
            if result.text != turn.reply_shown:
                self._show(result.text, "bmo")
            if end_session:
                threading.Thread(target=self.end_session, args=("new_session",),
                                 name="bmo-session", daemon=True).start()
            saved = [c for c in getattr(result, "memory_changes", ())
                     if c.get("op") in ("create", "update")]
            if saved:
                self._emit({"type": "memory", "turn": turn.id, "changes": saved})
            for status in getattr(result, "statuses", ()):
                log.info("BMO status: %s", status)
            for action in getattr(result, "actions", ())[turn.played:]:
                self._queue_action(action, turn, gen)   # not streamed (older server)
            # The reply starts once the sounds are done; animations go on over the speech.
            if not self._drain(turn.sounds, gen):
                return
            song = getattr(result, "song", None) or turn.song
            music_start_s = getattr(result, "music_start_s", None)
            self._reply_face(turn, getattr(result, "emotion", "neutral"), song, result.text)

            reply = None
            if result.audio_wav:
                reply = self.replies.save(turn.id, result.audio_wav)
                self._emit({"type": "audio", "turn": turn.id, "url": f"/api/audio/{turn.id}.wav",
                            "client_id": turn.client_id, "source": turn.source,
                            "duration": self._wav_duration(result.audio_wav)})
            if turn.reply_stream is not None:
                # Already playing piece by piece; now every piece is in.
                if turn.music and music_start_s is None and not song:
                    self._overlay(None, True, "")   # older server: no start time, music screen now
                turn.reply_stream.finish(music_start_s)
                turn.reply_stream.wait()
                spoke = turn.reply_stream.started
                if self._stale(gen):
                    return
            elif reply is not None and turn.play_on_pi:
                prepare = getattr(self.ui, "prepare_speech", None)
                if prepare is not None:
                    prepare(reply)          # lets the face follow the reply's loudness
                if turn.music and music_start_s is None and not song:
                    self._overlay(None, True, "")   # older server: no start time, music screen now
                if not self._set_state(BotState.SPEAKING, "", gen):
                    return
                spoke = True
                log.info("Reply audio starts (audio=whole), %.2f s after the request",
                         time.monotonic() - turn.sent_at)
                self._speak(reply, gen, music_start_s)
            elif turn.music_audio and turn.play_on_pi and not self.cfg.ui.text_only:
                # No reply audio, but the music action carries its track: play that.
                if not self._set_state(BotState.SPEAKING, "", gen):
                    return
                spoke = True
                path = self.cfg.runtime_path / "sfx" / "music.wav"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(turn.music_audio)
                self._speak(path, gen, 0.0)
            if not spoke and not self._drain(turn.anims, gen):
                return      # nothing spoken on the Pi: let the face show finish first
            ok = self._set_state(BotState.IDLE, "", gen)
        except _Stale:
            log.info("Turn interrupted while its reply streamed")
        except RequestCancelled:
            log.info("Request cancelled")
            self.notify("cancelled", gen=gen)
        except AuthError as e:
            log.error("Authentication failed: %s", e)
            self.notify("auth_failed", gen=gen)
        except ServerBusy as e:
            log.warning("Server busy: %s", e.code)
            self.notify(SERVER_CODES.get(e.code, "server_switching"), gen=gen,
                        server_message=e.display_message)
        except BadResponse as e:
            if e.status == 422 and "text or audio" in e.detail:
                # Whisper heard no words in the recording.
                self.notify("nothing_heard", gen=gen)
            else:
                log.error("Bad server response: %s %s", e, e.detail)
                self.notify("unknown", gen=gen)
        except ReservationTimeout as e:
            log.error("Readiness timeout: %s", e)
            self.notify("gpu_wait_timeout", gen=gen)
        except ServerUnavailable as e:
            log.error("Server unavailable: %s", e)
            self.notify("server_unreachable", gen=gen)
            self._start_health_retry()
        except Exception:
            log.exception("Interaction failed")
            self.notify("unknown", gen=gen)
        finally:
            for lane in (turn.anims, turn.sounds):
                if lane is not None:
                    lane.close()
            if turn.reply_stream is not None:
                turn.reply_stream.abort()       # no-op once it has played
            self._finish_turn(turn, ok, spoke)
            if own_audio and audio is not None:
                self._delete(audio)

    # --------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_listen_timer()
        self._health_stop.set()
        self._session_stop.set()
        if self._state is BotState.LISTENING:
            try:
                self.mic.abort()
            except Exception:
                pass
        if self._state in BUSY:
            self.interrupt(followup=False)
        try:
            self.speaker.stop()
        except Exception:
            pass
        self.reservation.release()
        self._idle.set()
        log.info("Controller shut down")

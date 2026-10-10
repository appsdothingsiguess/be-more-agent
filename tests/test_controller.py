import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Config
from app.controller import InteractionController
from app.hardware.camera import CameraError
from app.hardware.input import Action
from app.server.errors import AuthError, ServerUnavailable
from app.ui.states import BotState

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt "


class Log(list):
    """Shared, ordered event log across fakes."""


class FakeUI:
    def __init__(self, log):
        self.log, self.states, self.texts = log, [], []

    def set_state(self, state, message=""):
        self.states.append(state)

    def show_text(self, text, who="bmo"):
        self.texts.append((who, text))


class FakeClient:
    def __init__(self, log):
        self.log = log
        self.n = 0
        self.block = None  # threading.Event the next interact waits on
        self.entered = threading.Event()
        self.cancelled = threading.Event()
        self.calls = []
        self.error = None
        self.raw = {}
        self.actions = ()
        self.stream = []          # events fed to on_event before the result
        self.extra = {}           # more result attributes (song, memory_changes, ...)
        self.consolidated = []
        self.consolidate_result = True
        self.session_id = None
        self.turn_sessions = []   # X-Session-ID each interact was sent under
        self.consolidate_error = None

    def health(self):
        return {"ok": True}

    def new_request_id(self):
        self.n += 1
        return f"req-{self.n}"

    def interact(self, **kw):
        self.calls.append(kw)
        self.turn_sessions.append(self.session_id)
        self.log.append(("interact", kw["request_id"]))
        self.entered.set()
        if self.block is not None:
            self.block.wait(5)
        if self.error:
            raise self.error
        streamed = []
        for ev in self.stream:
            kw["on_event"](ev)
            if ev["type"] == "action":
                streamed.append(ev["action"])
        return SimpleNamespace(**{"transcript": "hi bmo", "text": f"reply to {kw['request_id']}",
                                  "audio_wav": WAV, "request_id": kw["request_id"],
                                  "raw": self.raw, "actions": tuple(streamed) or self.actions,
                                  **self.extra})

    def forget_memories(self):
        self.log.append(("forget_memories",))
        return 3

    def end_session(self, session_id):
        if self.consolidate_error:
            raise self.consolidate_error
        self.consolidated.append({"session_id": session_id})
        return self.consolidate_result

    def cancel(self, request_id):
        self.log.append(("cancel", request_id))
        self.cancelled.set()
        return True


class FakeReservation:
    def __init__(self, log):
        self.log = log

    def ensure_ready(self):
        self.log.append(("ensure_ready",))

    def mark_stale(self):
        self.log.append(("mark_stale",))

    def renewed(self):
        self.log.append(("renewed",))

    def release(self):
        self.log.append(("release",))


class FakeMic:
    def __init__(self, log, result):
        self.log, self.result = log, result

    def configure(self):
        self.log.append(("mic_configure",))

    def start(self):
        self.log.append(("mic_start",))

    def stop(self):
        self.log.append(("mic_stop",))
        return self.result

    def abort(self):
        self.log.append(("mic_abort",))


class FakeSpeaker:
    def __init__(self, log):
        self.log = log
        self.played = []
        self.block = None
        self.playing = threading.Event()
        self._stop = threading.Event()
        self.streams = []
        self.stream_play_s = 0.0

    def play(self, path, block=True):
        self.played.append(Path(path))
        self.log.append(("play", Path(path).name))
        self._stop.clear()
        self.playing.set()
        if self.block is not None:
            self._stop.wait(5)
            return False
        return True

    def play_bytes(self, data, path, block=True):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(data)
        return self.play(path, block=block)

    def configure(self):
        self.log.append(("speaker_configure",))

    def play_effect(self, category):
        self.log.append(("effect", category))

    def stop(self):
        self.log.append(("speaker_stop",))
        self._stop.set()

    def open_stream(self, rate, channels=1):
        self._stop.clear()
        self.log.append(("stream", rate))
        st = FakeStream(self)
        self.streams.append(st)
        return st


class FakeStream:
    """An open speaker stream: logs each write; close() 'plays' for play_s seconds, or
    until stop() when play_s is None."""

    def __init__(self, spk):
        self.spk, self.writes, self.closed, self.aborted = spk, [], False, False

    def write(self, pcm):
        if self.spk._stop.is_set():
            return False
        self.writes.append((time.monotonic(), pcm))
        return True

    def close(self):
        self.closed = True
        if self.spk.stream_play_s is None:
            self.spk._stop.wait(5)
        else:
            self.spk._stop.wait(self.spk.stream_play_s)
        self.spk.log.append(("stream_end", time.monotonic()))
        return not self.spk._stop.is_set()

    def abort(self):
        self.aborted = True


class FakeCamera:
    def __init__(self, path, fail=False):
        self.path, self.fail, self.n = path, fail, 0

    def capture(self):
        self.n += 1
        if self.fail:
            raise CameraError("no camera")
        return self.path


@pytest.fixture
def rig(tmp_path):
    log = Log()
    cfg = Config(runtime_dir=str(tmp_path / "runtime"))
    cfg.camera.vision_mode = "always"
    audio = tmp_path / "up.wav"
    audio.write_bytes(WAV)
    img = tmp_path / "cam.jpg"
    img.write_bytes(b"jpg")
    r = SimpleNamespace(
        log=log, cfg=cfg, ui=FakeUI(log), client=FakeClient(log), res=FakeReservation(log),
        mic=FakeMic(log, audio), spk=FakeSpeaker(log), cam=FakeCamera(img), audio=audio, img=img,
    )
    r.ctl = InteractionController(cfg, r.ui, r.client, r.res, r.mic, r.spk, r.cam)
    r.ctl.start()
    return r


def names(log):
    return [e[0] for e in log]


def test_start_configures_mic_and_speaker(rig):
    assert ("mic_configure",) in rig.log and ("speaker_configure",) in rig.log


def test_full_turn_happy_path(rig):
    rig.ctl.handle_action(Action.START)
    assert rig.ctl.state is BotState.LISTENING
    rig.ctl.handle_action(Action.START)
    assert rig.ctl.wait_idle(5)
    assert rig.ctl.state is BotState.IDLE
    call = rig.client.calls[0]
    assert call["audio_path"] == rig.audio and call["image_path"] == rig.img
    assert call["speak"] is True and call["request_id"] == "req-1"
    seq = [s for s in rig.ui.states]
    for a, b in [(BotState.LISTENING, BotState.CAPTURING), (BotState.CAPTURING, BotState.THINKING),
                 (BotState.THINKING, BotState.SPEAKING), (BotState.SPEAKING, BotState.IDLE)]:
        assert seq.index(a) < len(seq) - 1 - seq[::-1].index(b)
    n = names(rig.log)
    assert n.index("ensure_ready") < n.index("interact") < n.index("renewed") < n.index("play")
    assert ("user", "hi bmo") in rig.ui.texts and ("bmo", "reply to req-1") in rig.ui.texts
    assert rig.spk.played[-1].read_bytes() == WAV


def test_nothing_heard_skips_server(rig):
    rig.mic.result = None
    rig.ctl.handle_action(Action.START)
    rig.ctl.handle_action(Action.START)
    assert rig.ctl.wait_idle(5)
    rig.ctl.wait_idle(5)
    assert rig.client.calls == []
    assert "ensure_ready" not in names(rig.log)


def test_interrupt_during_thinking_stops_cancels_and_drops(rig):
    rig.client.block = threading.Event()
    rig.ctl.handle_action(Action.START)
    rig.ctl.handle_action(Action.START)
    assert rig.client.entered.wait(5)
    rig.ctl.handle_action(Action.START)  # Start during THINKING = interrupt
    assert rig.client.cancelled.wait(5)
    assert rig.ctl.state is BotState.IDLE
    n = names(rig.log)
    assert n.index("speaker_stop") < n.index("cancel")  # stop playback before cancelling
    assert ("cancel", "req-1") in rig.log

    played_before = len(rig.spk.played)
    rig.client.block.set()  # the old response arrives late...
    for t in threading.enumerate():
        if t.name.startswith("bmo-turn"):
            t.join(5)
    assert len(rig.spk.played) == played_before  # ...and is never played
    assert ("bmo", "reply to req-1") not in rig.ui.texts
    assert "renewed" not in names(rig.log)
    assert rig.ctl.state is BotState.IDLE


def test_next_turn_after_cancel_rechecks_readiness_with_new_id(rig):
    rig.client.block = threading.Event()
    rig.ctl.handle_action(Action.START)
    rig.ctl.handle_action(Action.START)
    assert rig.client.entered.wait(5)
    rig.ctl.interrupt()
    rig.client.block.set()
    rig.client.block = None
    rig.log.clear()

    rig.ctl.handle_action(Action.START)
    rig.ctl.handle_action(Action.START)
    for t in threading.enumerate():
        if t.name.startswith("bmo-turn"):
            t.join(5)
    n = names(rig.log)
    assert n.index("mark_stale") < n.index("ensure_ready") < n.index("interact")
    ids = [c["request_id"] for c in rig.client.calls]
    assert ids == ["req-1", "req-2"]  # never reused
    assert ("bmo", "reply to req-2") in rig.ui.texts


def test_interrupt_during_speaking_stops_playback(rig):
    rig.spk.block = True
    rig.ctl.handle_action(Action.START)
    rig.ctl.handle_action(Action.START)
    assert rig.spk.playing.wait(5)
    assert rig.ctl.state is BotState.SPEAKING
    rig.ctl.handle_action(Action.INTERRUPT)
    assert rig.ctl.state is BotState.IDLE
    assert ("speaker_stop",) in rig.log
    assert "cancel" not in names(rig.log)  # request already finished; nothing to cancel


def test_vision_modes(rig):
    rig.cfg.camera.vision_mode = "off"
    rig.ctl.submit_text("hello")
    rig.ctl.wait_idle(5)
    _join_turns()
    assert rig.client.calls[-1]["image_path"] is None and rig.cam.n == 0

    rig.cfg.camera.vision_mode = "manual"
    rig.ctl.submit_text("hello")
    _join_turns()
    assert rig.client.calls[-1]["image_path"] is None
    rig.ctl.handle_action(Action.B)  # arm
    rig.ctl.submit_text("what do you see")
    _join_turns()
    assert rig.client.calls[-1]["image_path"] == rig.img
    assert rig.client.calls[-1]["text"] == "what do you see"
    rig.ctl.submit_text("again")
    _join_turns()
    assert rig.client.calls[-1]["image_path"] is None  # arming is one-shot


def test_camera_failure_continues_without_image(rig):
    rig.cam.fail = True
    rig.ctl.submit_text("hi")
    _join_turns()
    assert rig.client.calls[-1]["image_path"] is None
    assert rig.ctl.state is BotState.IDLE


def test_auth_error_shows_error_state(rig):
    rig.client.error = AuthError("401")
    rig.ctl.submit_text("hi")
    _join_turns()
    assert rig.ctl.state is BotState.ERROR
    # Start from ERROR begins a new recording
    rig.ctl.handle_action(Action.START)
    assert rig.ctl.state is BotState.LISTENING


def test_interrupt_while_listening_aborts(rig):
    rig.ctl.handle_action(Action.START)
    rig.ctl.handle_action(Action.INTERRUPT)
    assert rig.ctl.state is BotState.IDLE
    assert ("mic_abort",) in rig.log and rig.client.calls == []


def test_shutdown_releases_reservation_once(rig):
    rig.ctl.shutdown()
    rig.ctl.shutdown()
    assert names(rig.log).count("release") == 1
    rig.ctl.handle_action(Action.START)  # ignored after shutdown
    assert ("mic_start",) not in rig.log


SOUNDS = Path(__file__).resolve().parent.parent / "sounds"


def test_text_only_never_plays_and_asks_for_no_audio(rig):
    rig.cfg.ui.text_only = True
    rig.ctl.submit_text("hi")
    _join_turns()
    assert rig.client.calls[-1]["speak"] is False
    assert "play" not in names(rig.log) and ("effect", "ack") not in rig.log
    assert ("bmo", "reply to req-1") in rig.ui.texts


def test_per_message_speak_overrides_mute(rig):
    rig.cfg.ui.text_only = True
    rig.ctl.submit_text("hi", speak=True)
    _join_turns()
    assert rig.client.calls[-1]["speak"] is True and "play" in names(rig.log)


def test_server_busy_shows_server_message_and_plays_clip(rig):
    from app.server.errors import ServerBusy
    rig.spk.sounds_dir = SOUNDS
    events = []
    rig.ctl.subscribe(events.append)
    rig.client.error = ServerBusy("large_model_session_active", "Busy with a large model.", 503)
    rig.ctl.submit_text("hi")
    _join_turns()
    assert rig.ctl.state is BotState.ERROR
    err = [e for e in events if e["type"] == "error"][-1]
    assert err["code"] == "server_busy_large_model" and err["message"] == "Busy with a large model."
    assert ("play", "server_busy_large_model.wav") in rig.log
    assert err in rig.ctl.history


def test_error_clip_muted_in_text_only(rig):
    rig.spk.sounds_dir = SOUNDS
    rig.cfg.ui.text_only = True
    rig.client.error = AuthError("401")
    rig.ctl.submit_text("hi")
    _join_turns()
    assert rig.ctl.state is BotState.ERROR
    assert not any(e[0] == "play" for e in rig.log)
    assert any("doesn't recognise me" in t for _, t in rig.ui.texts)


def test_nothing_heard_is_soft(rig):
    rig.spk.sounds_dir = SOUNDS
    rig.mic.result = None
    rig.ctl.handle_action(Action.START)
    rig.ctl.handle_action(Action.START)
    _join_turns()
    assert rig.ctl.state is BotState.IDLE
    assert ("play", "nothing_heard.wav") in rig.log


def test_server_heard_no_words_is_nothing_heard(rig):
    from app.server.errors import BadResponse
    rig.spk.sounds_dir = SOUNDS
    rig.client.error = BadResponse("unexpected status 422", status=422,
                                   detail='{"detail":"Provide text or audio"}')
    rig.ctl.handle_action(Action.START)
    rig.ctl.handle_action(Action.START)
    _join_turns()
    assert rig.ctl.state is BotState.IDLE
    assert ("play", "nothing_heard.wav") in rig.log


def test_waiting_notice_once_per_turn(rig):
    events = []
    rig.ctl.subscribe(events.append)

    def slow_ready():
        rig.log.append(("ensure_ready",))
        rig.ctl._on_server_wait({"scheduling_state": "restoring_bmo"})
        rig.ctl._on_server_wait({"scheduling_state": "restoring_bmo"})
    rig.res.ensure_ready = slow_ready
    rig.ctl.submit_text("hi")
    _join_turns()
    notices = [e for e in events if e["type"] == "notice"]
    assert len(notices) == 1 and notices[0]["code"] == "server_waiting"
    assert rig.ctl.state is BotState.IDLE  # the turn carried on


def test_events_and_history(rig):
    events = []
    unsubscribe = rig.ctl.subscribe(events.append)
    rig.ctl.submit_text("hi")
    _join_turns()
    types = [e["type"] for e in events]
    assert "state" in types and "text" in types
    assert [e["who"] for e in rig.ctl.history if e["type"] == "text"][-2:] == ["user", "bmo"]
    unsubscribe()
    n = len(events)
    rig.ctl.submit_text("again")
    _join_turns()
    assert len(events) == n


def test_apply_setting_volume_persists(rig):
    from app.settings import SettingError, settings_path
    assert rig.ctl.apply_setting("speaker.volume", 80) == "80%"
    assert names(rig.log).count("speaker_configure") == 2
    assert settings_path(rig.cfg).is_file()
    with pytest.raises(SettingError):
        rig.ctl.apply_setting("server_url", "http://x")


def test_server_status_summary_cached(rig):
    calls = []
    rig.client.status = lambda: calls.append(1) or {"bmo_ready": True, "scheduling_state": "normal",
                                                     "queued_requests": 0, "active_requests": 1}
    a = rig.ctl.server_status()
    b = rig.ctl.server_status()
    assert a == b and len(calls) == 1
    assert a["reachable"] and a["bmo_ready"] and a["mode"] == "normal"


def _join_turns():
    for t in threading.enumerate():
        if t.name.startswith("bmo-turn"):
            t.join(5)


def run_text(rig, text="hello"):
    rig.ctl.submit_text(text)
    assert rig.client.entered.wait(5)
    assert rig.ctl.wait_idle(5)
    rig.client.entered.clear()


def test_history_sent_and_exchange_stored(rig):
    run_text(rig, "first")
    assert rig.client.calls[0]["history"] == [] and rig.client.calls[0]["memory"] is True
    assert rig.ctl.conversation() == [{"role": "user", "content": "hi bmo"},
                                      {"role": "assistant", "content": "reply to req-1"}]
    run_text(rig, "second")
    assert len(rig.client.calls[1]["history"]) == 2
    assert len(rig.ctl.conversation()) == 4


def test_memory_disabled_sends_off_and_stores_nothing(rig):
    rig.cfg.memory.enabled = False
    run_text(rig)
    call = rig.client.calls[0]
    assert call["memory"] is False and "history" not in call
    assert rig.ctl.conversation() == []


def test_memory_reset_clears_local(rig):
    run_text(rig)
    assert len(rig.ctl.conversation()) == 2
    rig.client.raw = {"memory_reset": True}
    run_text(rig)
    assert rig.ctl.conversation() == []


def test_typed_forget_never_calls_interact(rig):
    run_text(rig)
    rig.ctl.submit_text("BMO, forget everything!")
    for _ in range(100):
        if ("forget_memories",) in rig.log:
            break
        threading.Event().wait(0.05)
    assert ("forget_memories",) in rig.log
    assert len(rig.client.calls) == 1
    assert rig.ctl.conversation() == []
    assert ("user", "BMO, forget everything!") in rig.ui.texts
    assert ("bmo", "Okay! BMO forgot everything.") in rig.ui.texts


def test_forget_during_turn_means_turn_not_stored(rig):
    rig.client.block = threading.Event()
    rig.ctl.submit_text("hello")
    assert rig.client.entered.wait(5)
    result = rig.ctl.forget_memory()
    assert result == {"local_cleared": True, "server": "ok", "forgotten": 3}
    rig.client.block.set()
    assert rig.ctl.wait_idle(5)
    assert rig.ctl.conversation() == []


def test_failed_turn_stores_nothing(rig):
    rig.client.error = AuthError("nope")
    run_text(rig)
    assert rig.ctl.conversation() == []


def test_public_memory_api_never_raises(rig):
    rig.client.list_memories = lambda: None
    assert rig.ctl.list_memories() == {"available": False, "memories": [], "error": None}
    rig.client.list_memories = lambda: [{"id": 1}]
    assert rig.ctl.list_memories()["available"] is True

    def boom(*a):
        raise AuthError("x")
    rig.client.list_memories = boom
    rig.client.forget_memories = boom
    rig.client.delete_memory = boom
    assert rig.ctl.list_memories()["error"] == "AuthError"
    assert rig.ctl.forget_memory()["server"] == "error"
    assert rig.ctl.delete_memory("likes-tea") == {"available": True, "deleted": False}
    rig.client.delete_memory = lambda i: None
    assert rig.ctl.delete_memory("likes-tea") == {"available": False, "deleted": False}
    rig.client.forget_memories = lambda: None
    assert rig.ctl.forget_memory()["server"] == "unavailable"


# ---------------------------------------------------------- W1 contracts
import re
import time
import wave

TID = re.compile(r"^[0-9a-f]{16}$")


def real_wav(seconds=0.5, rate=8000):
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(rate * seconds))
    return buf.getvalue()


def collect(ctl):
    events = []
    ctl.subscribe(events.append)
    return events


def of(events, kind):
    return [e for e in events if e["type"] == kind]


def wait_for(cond, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def health_threads():
    return [t for t in threading.enumerate() if t.name == "bmo-health"]


def test_submit_text_returns_turn_id_and_tags_events(rig):
    events = collect(rig.ctl)
    tid = rig.ctl.submit_text("hi", source="web", client_id="c1")
    _join_turns()
    assert TID.match(tid)
    assert rig.ctl.submit_text("   ") is None
    assert all(e["turn"] == tid for e in of(events, "text"))
    assert any(e["turn"] == tid and e["source"] == "web" for e in of(events, "state"))
    phases = of(events, "phase")
    assert [e["phase"] for e in phases] == ["looking", "thinking", "speaking", "idle"]
    assert all(e["client_id"] == "c1" and e["turn"] == tid for e in phases)
    done = of(events, "turn_done")
    assert len(done) == 1
    assert (done[0]["turn"], done[0]["source"], done[0]["client_id"]) == (tid, "web", "c1")
    assert done[0]["ok"] is True and done[0]["spoke_on_pi"] is True


def test_forget_command_returns_none(rig):
    assert rig.ctl.submit_text("BMO, forget everything!") is None


def test_audio_event_before_playback_and_reply_saved(rig):
    rig.client.interact = lambda **kw: SimpleNamespace(
        transcript="t", text="r", audio_wav=real_wav(), request_id="x", raw={})
    order = []
    rig.ctl.subscribe(lambda e: order.append("audio") if e["type"] == "audio" else None)
    orig = rig.spk.play

    def play(p, block=True):
        order.append("play")
        return orig(p, block)
    rig.spk.play = play
    tid = rig.ctl.submit_text("hi")
    _join_turns()
    assert order == ["audio", "play"]
    assert rig.ctl.reply_path(tid).read_bytes() == real_wav()
    assert rig.ctl.reply_path("../etc") is None


def test_audio_event_fields_and_duration(rig):
    rig.client.interact = lambda **kw: SimpleNamespace(
        transcript="t", text="r", audio_wav=real_wav(seconds=0.5), request_id="x", raw={})
    events = collect(rig.ctl)
    tid = rig.ctl.submit_text("hi", client_id="c9", source="live")
    _join_turns()
    a = of(events, "audio")[0]
    assert a["url"] == f"/api/audio/{tid}.wav" and a["turn"] == tid
    assert a["client_id"] == "c9" and a["source"] == "live" and a["duration"] == 0.5


def test_audio_duration_none_when_unreadable(rig):
    events = collect(rig.ctl)
    rig.ctl.submit_text("hi")      # fake WAV bytes have a truncated header
    _join_turns()
    assert of(events, "audio")[0]["duration"] is None


def test_play_on_pi_false_skips_speaking_but_saves_reply(rig):
    events = collect(rig.ctl)
    tid = rig.ctl.submit_text("hi", speak=True, play_on_pi=False)
    _join_turns()
    assert rig.client.calls[-1]["speak"] is True
    assert rig.spk.played == [] and BotState.SPEAKING not in rig.ui.states
    assert "speaking" not in [e["phase"] for e in of(events, "phase")]
    assert rig.ctl.state is BotState.IDLE and rig.ctl.reply_path(tid) is not None
    assert of(events, "turn_done")[0]["spoke_on_pi"] is False
    assert ("effect", "ack") not in rig.log


def test_speak_false_requests_no_audio(rig):
    rig.ctl.submit_text("hi", speak=False)
    _join_turns()
    assert rig.client.calls[-1]["speak"] is False and rig.spk.played == []


def test_submit_audio_owns_and_deletes_file(rig, tmp_path):
    up = tmp_path / "web.webm"
    up.write_bytes(b"audio")
    events = collect(rig.ctl)
    tid = rig.ctl.submit_audio(up, play_on_pi=False, client_id="c2")
    _join_turns()
    assert TID.match(tid) and rig.client.calls[-1]["audio_path"] == up
    assert not up.exists()
    assert of(events, "turn_done")[0]["client_id"] == "c2"


def test_submit_audio_deleted_on_error(rig, tmp_path):
    up = tmp_path / "web.webm"
    up.write_bytes(b"audio")
    rig.client.error = AuthError("no")
    events = collect(rig.ctl)
    rig.ctl.submit_audio(up)
    _join_turns()
    assert not up.exists()
    assert of(events, "turn_done")[0]["ok"] is False


def test_pi_mic_file_not_deleted(rig):
    rig.ctl.start_listening()
    rig.ctl.finish_listening()
    _join_turns()
    assert rig.ctl.wait_idle(5)
    assert rig.audio.exists()


def test_start_listening_sources_and_guards(rig):
    assert rig.ctl.start_listening("bogus") is False
    events = collect(rig.ctl)
    assert rig.ctl.start_listening("wake") is True
    assert rig.ctl.listening_source == "wake" and rig.ctl.state is BotState.LISTENING
    assert rig.ctl.start_listening("button") is False      # already listening
    ph = of(events, "phase")[0]
    assert ph["phase"] == "listening" and ph["source"] == "wake" and ph["turn"] is None
    rig.ctl.finish_listening()
    _join_turns()
    done = of(events, "turn_done")[0]
    assert done["source"] == "wake" and done["ok"] and done["spoke_on_pi"]
    assert rig.ctl.listening_source is None


def test_start_listening_refused_while_busy(rig):
    rig.client.block = threading.Event()
    rig.ctl.submit_text("hi")
    assert rig.client.entered.wait(5)
    assert rig.ctl.start_listening("followup") is False
    rig.client.block.set()
    _join_turns()


def test_cancel_listening_quiet(rig):
    events = collect(rig.ctl)
    rig.ctl.start_listening("followup")
    rig.ctl.cancel_listening(quiet=True)
    assert rig.ctl.state is BotState.IDLE and ("mic_abort",) in rig.log
    assert of(events, "state")[-1]["message"] == rig.ctl.idle_hint
    rig.ctl.start_listening()
    rig.ctl.cancel_listening()
    assert of(events, "state")[-1]["message"] == "Cancelled"


def test_interrupt_aborts_listening(rig):
    rig.ctl.start_listening()
    rig.ctl.interrupt()
    assert rig.ctl.state is BotState.IDLE and ("mic_abort",) in rig.log


def test_interrupt_emits_turn_done_once(rig):
    rig.client.block = threading.Event()
    events = collect(rig.ctl)
    tid = rig.ctl.submit_text("hi")
    assert rig.client.entered.wait(5)
    rig.ctl.interrupt()
    rig.client.block.set()
    _join_turns()
    done = of(events, "turn_done")
    assert len(done) == 1 and done[0]["turn"] == tid and done[0]["ok"] is False


def test_waiting_gpu_phase_once(rig):
    events = collect(rig.ctl)

    def slow_ready():
        rig.ctl._on_server_wait({})
        rig.ctl._on_server_wait({})
    rig.res.ensure_ready = slow_ready
    tid = rig.ctl.submit_text("hi")
    _join_turns()
    waits = [e for e in of(events, "phase") if e["phase"] == "waiting_gpu"]
    assert len(waits) == 1 and waits[0]["turn"] == tid
    assert len([e for e in of(events, "notice") if e["code"] == "server_waiting"]) == 1


def test_snapshot_sticky_and_history_only_text_error(rig):
    assert rig.ctl.snapshot()[0]["phase"] == "idle"
    rig.ctl.publish({"type": "live", "armed": True, "model": "hey_jarvis", "error": None})
    rig.ctl.publish({"type": "live", "armed": False, "model": "hey_jarvis", "error": "x"})
    snap = rig.ctl.snapshot()
    assert [e["type"] for e in snap] == ["phase", "live"] and snap[1]["armed"] is False
    rig.ctl.start_listening()
    assert rig.ctl.snapshot()[0]["phase"] == "listening"
    rig.ctl.cancel_listening()
    rig.ctl.submit_text("hi")
    _join_turns()
    assert rig.ctl.history and all(e["type"] in ("text", "error") for e in rig.ctl.history)


def test_apply_setting_publishes_event(rig):
    events = collect(rig.ctl)
    rig.ctl.apply_setting("listen.followup", False)
    ev = of(events, "setting")
    assert len(ev) == 1 and ev[0]["key"] == "listen.followup" and ev[0]["value"] is False


def make_ctl(tmp_path, health):
    log = Log()
    client = FakeClient(log)
    client.health = health
    ctl = InteractionController(Config(runtime_dir=str(tmp_path / "runtime")), FakeUI(log), client,
                                FakeReservation(log), FakeMic(log, None), FakeSpeaker(log))
    ctl.health_backoff = (0.01, 0.02)
    return ctl, log


def test_phases_starting_then_error(tmp_path):
    def down():
        raise ServerUnavailable("down")
    ctl, _ = make_ctl(tmp_path, down)
    events = collect(ctl)
    ctl.start()
    assert [e["phase"] for e in of(events, "phase")][:2] == ["starting", "error"]
    ctl.shutdown()


def test_controller_purges_legacy_and_keeps_eight_replies(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "uploads").mkdir(parents=True)
    (runtime / "reply-3.wav").write_bytes(b"x")
    (runtime / "uploads" / "u.wav").write_bytes(b"x")
    ctl, _ = make_ctl(tmp_path, lambda: {})
    assert not (runtime / "reply-3.wav").exists() and not (runtime / "uploads" / "u.wav").exists()
    assert ctl.replies.keep == 8 and ctl.replies.dir == runtime / "replies"


def test_prewarm_single_flight_and_never_raises(rig):
    gate = threading.Event()
    calls = []

    def ready():
        calls.append(1)
        gate.wait(5)
        raise RuntimeError("boom")
    rig.res.ensure_ready = ready
    rig.ctl.prewarm()
    rig.ctl.prewarm()
    assert wait_for(lambda: calls == [1])
    time.sleep(0.05)
    assert calls == [1]
    gate.set()
    assert wait_for(lambda: not rig.ctl._prewarming)
    rig.ctl.prewarm()      # usable again
    assert wait_for(lambda: len(calls) == 2)


def test_health_retry_after_startup_failure(tmp_path):
    attempts = []

    def health():
        attempts.append(1)
        if len(attempts) < 3:
            raise ServerUnavailable("down")
        return {"ok": True}
    ctl, log = make_ctl(tmp_path, health)
    ctl.start()                                   # attempt 1 fails
    assert ctl.state is BotState.ERROR
    assert wait_for(lambda: ctl.state is BotState.IDLE)
    assert len(attempts) == 3
    assert ("effect", "greeting") in log          # startup greeting on recovery
    assert wait_for(lambda: not health_threads())
    ctl.shutdown()


def test_health_retry_after_server_unreachable_turn(rig):
    rig.ctl.health_backoff = (0.01, 0.02)
    rig.client.error = ServerUnavailable("gone")
    rig.ctl.submit_text("hi")
    _join_turns()
    assert rig.ctl.state is BotState.ERROR
    assert wait_for(lambda: rig.ctl.state is BotState.IDLE)
    assert names(rig.log).count("effect") == 2   # startup greeting + ack only, no second greeting


def test_health_retry_stops_on_shutdown(tmp_path):
    def down():
        raise ServerUnavailable("down")
    ctl, _ = make_ctl(tmp_path, down)
    ctl.start()
    assert health_threads()
    ctl.shutdown()
    assert wait_for(lambda: not health_threads())


# -- sessions -------------------------------------------------------------
def events_of(rig, kind="session"):
    got = []
    rig.ctl.subscribe(lambda ev: got.append(ev) if ev["type"] == kind else None)
    return got


def session_ctl(tmp_path, client=None):
    log = Log()
    cfg = Config(runtime_dir=str(tmp_path / "runtime"))
    client = client or FakeClient(log)
    ctl = InteractionController(cfg, FakeUI(log), client, FakeReservation(log),
                                FakeMic(log, None), FakeSpeaker(log))
    return ctl, client


def test_exchange_goes_to_session(rig):
    run_text(rig, "one")
    run_text(rig, "two")
    assert len(rig.ctl.session) == 4
    assert rig.ctl.session_info()["messages"] == 4
    assert (Path(rig.cfg.runtime_dir) / "session.json").is_file()


def test_new_session_consolidates_and_resets(rig):
    run_text(rig, "one")
    run_text(rig, "two")
    old = rig.ctl.session.id
    got = events_of(rig)
    out = rig.ctl.new_session()
    assert rig.client.consolidated == [{"session_id": old}]
    assert rig.client.turn_sessions == [old, old]     # the server logged both turns under it
    assert out["consolidated"] == "ok" and out["id"] == rig.ctl.session.id != old
    assert rig.client.session_id == out["id"]         # later turns go to the new session
    assert got[0]["id"] == out["id"] and got[0]["reason"] == "new_session"
    assert got[0]["consolidated"] == "ok"
    assert rig.ctl.conversation() == [] and rig.ctl.session.is_empty
    assert not any(e["type"] == "text" for e in rig.ctl.history)


def test_new_session_unavailable_still_resets(rig):
    run_text(rig)
    rig.client.consolidate_result = None
    out = rig.ctl.new_session()
    assert out["consolidated"] == "unavailable"
    assert rig.ctl.session.is_empty and rig.ctl.conversation() == []
    assert not list(rig.ctl.pending_dir.glob("*.json"))


def test_new_session_empty_is_skipped(rig):
    assert rig.ctl.new_session()["consolidated"] == "skipped"
    assert rig.client.consolidated == []


def test_failure_goes_pending_then_retried(rig):
    run_text(rig, "first")
    first = rig.ctl.session.id
    rig.client.consolidate_error = ServerUnavailable("down")
    got = events_of(rig)
    assert rig.ctl.end_session("new_session")["consolidated"] == "pending"
    assert got[0]["consolidated"] == "pending"
    assert [f.stem for f in rig.ctl.pending_dir.glob("*.json")] == [first]
    rig.client.consolidate_error = None
    run_text(rig, "second")
    second = rig.ctl.session.id
    assert rig.ctl.end_session("idle")["consolidated"] == "ok"
    assert [c["session_id"] for c in rig.client.consolidated] == [second, first]
    assert not list(rig.ctl.pending_dir.glob("*.json"))


def test_pending_capped_at_twenty(rig):
    rig.client.consolidate_error = ServerUnavailable("down")
    ids = []
    for _ in range(22):
        rig.ctl.session.add_exchange("q", "a")
        ids.append(rig.ctl.session.id)
        rig.ctl.end_session("idle")
        f = rig.ctl.pending_dir / f"{ids[-1]}.json"
        if f.exists():
            os.utime(f, (len(ids), len(ids)))
    assert sorted(f.stem for f in rig.ctl.pending_dir.glob("*.json")) == sorted(ids[2:])


def test_retry_pending_when_server_returns(rig):
    rig.client.consolidate_error = ServerUnavailable("down")
    rig.ctl.session.add_exchange("q", "a")
    rig.ctl.end_session("idle")
    assert len(list(rig.ctl.pending_dir.glob("*.json"))) == 1
    assert rig.ctl._retry_pending() == 0
    rig.client.consolidate_error = None
    assert rig.ctl._retry_pending() == 1
    assert not list(rig.ctl.pending_dir.glob("*.json"))


def test_memory_disabled_skips_but_resets(rig):
    rig.ctl.session.add_exchange("q", "a")
    rig.cfg.memory.enabled = False
    out = rig.ctl.end_session("new_session")
    assert out["consolidated"] == "skipped" and rig.client.consolidated == []
    assert rig.ctl.session.is_empty


def test_idle_trigger(rig):
    clock = [1000.0]
    rig.cfg.memory.session_idle_minutes = 5
    rig.ctl.session._clock = lambda: clock[0]
    rig.ctl.session.last_activity = 1000.0
    rig.ctl.session.add_exchange("q", "a")
    assert rig.ctl._idle_tick() is False                  # fresh
    clock[0] += 299
    assert rig.ctl._idle_tick() is False
    clock[0] += 2
    assert rig.ctl._idle_tick() is True
    assert rig.client.consolidated and rig.ctl.session.is_empty


def test_idle_disabled_busy_and_empty(rig):
    clock = [0.0]
    rig.ctl.session._clock = lambda: clock[0]
    assert rig.ctl._idle_tick() is False                  # empty
    rig.ctl.session.add_exchange("q", "a")
    clock[0] = 10_000
    rig.cfg.memory.session_idle_minutes = 0
    assert rig.ctl._idle_tick() is False                  # disabled
    rig.cfg.memory.session_idle_minutes = 5
    rig.ctl._state = BotState.THINKING
    assert rig.ctl._idle_tick() is False                  # mid-turn
    rig.ctl._state = BotState.IDLE
    assert rig.ctl._idle_tick() is True


def test_idle_thread_runs(rig):
    rig.ctl._session_stop.set()
    rig.ctl._session_thread.join(2)
    rig.ctl._session_stop.clear()
    rig.ctl.session_poll = 0.02
    rig.ctl.session.add_exchange("q", "a")
    rig.ctl.session.last_activity = rig.ctl.session.last_activity - 3600
    rig.ctl._start_session_thread()
    wait_for(lambda: rig.client.consolidated)
    assert rig.client.consolidated
    rig.ctl.shutdown()


def test_turn_start_touches_activity(rig):
    rig.ctl.session.last_activity = 0.0
    run_text(rig)
    assert rig.ctl.session.idle_seconds() < 5


def test_startup_consolidates_stale_leftover(tmp_path):
    first, _ = session_ctl(tmp_path)
    first.session.add_exchange("old q", "old a")
    first.session.last_activity -= 3600
    first.session._save()
    old = first.session.id
    ctl, client = session_ctl(tmp_path)
    assert ctl.session.id == old
    ctl.start()
    wait_for(lambda: client.consolidated)
    assert client.consolidated[0]["session_id"] == old
    assert ctl.session.id != old
    ctl.shutdown()


def test_startup_keeps_recent_session(tmp_path):
    a, _ = session_ctl(tmp_path)
    a.session.add_exchange("q", "a")
    b, client = session_ctl(tmp_path)
    b.start()
    assert b.session.id == a.session.id and client.consolidated == []
    b.shutdown()


def test_forget_clears_session_and_pending(rig):
    rig.client.consolidate_error = ServerUnavailable("down")
    rig.ctl.session.add_exchange("q", "a")
    rig.ctl.end_session("idle")
    rig.ctl.session.add_exchange("q2", "a2")
    old = rig.ctl.session.id
    assert list(rig.ctl.pending_dir.glob("*.json"))
    rig.ctl.forget_memory()
    assert rig.ctl.session.is_empty and rig.ctl.session.id != old
    assert rig.client.session_id == rig.ctl.session.id
    assert not list(rig.ctl.pending_dir.glob("*.json"))


def test_typed_new_session_command(rig):
    run_text(rig)
    assert rig.ctl.submit_text("Hey BMO, new session please!") is None
    wait_for(lambda: rig.client.consolidated)
    assert rig.client.consolidated and len(rig.client.calls) == 1
    assert ("user", "Hey BMO, new session please!") in rig.ui.texts
    assert ("bmo", "Okay! Fresh start.") in rig.ui.texts


def test_spoken_new_session_command(rig):
    run_text(rig, "earlier")
    orig = rig.client.interact

    def interact(**kw):
        r = orig(**kw)
        r.transcript = "Start over."
        return r
    rig.client.interact = interact
    old = rig.ctl.session.id
    got = events_of(rig)
    run_text(rig, "x")
    wait_for(lambda: got)
    assert got and got[0]["reason"] == "new_session"
    assert rig.client.consolidated[0]["session_id"] == old
    assert rig.ctl.conversation() == []


def test_server_session_reset_starts_new_session(rig):
    run_text(rig, "earlier")
    old = rig.ctl.session.id
    rig.client.raw = {"session_reset": True}       # the server matched a phrase the Pi didn't
    got = events_of(rig)
    run_text(rig, "let's begin again")
    wait_for(lambda: got)
    assert got[0]["reason"] == "new_session" and rig.ctl.session.id != old
    assert rig.client.consolidated[0]["session_id"] == old


def test_old_pending_files_are_retried_by_session_id(rig):
    rig.ctl.pending_dir.mkdir(parents=True, exist_ok=True)
    (rig.ctl.pending_dir / "legacy.json").write_text(json.dumps(
        {"session_id": "legacy", "reason": "idle", "messages": [], "started_at": "a"}))
    assert rig.ctl._retry_pending() == 1
    assert rig.client.consolidated == [{"session_id": "legacy"}]


def test_goodbye_ends_session_without_followup(rig):
    run_text(rig, "earlier")
    orig = rig.client.interact

    def interact(**kw):
        r = orig(**kw)
        r.transcript = "Okay, thanks BMO. Bye!"
        return r
    rig.client.interact = interact
    old = rig.ctl.session.id
    done, got = [], events_of(rig)
    rig.ctl.subscribe(lambda e: done.append(e) if e["type"] == "turn_done" else None)
    run_text(rig, "x")
    wait_for(lambda: got)
    assert done[-1]["goodbye"] is True
    assert got[0]["reason"] == "goodbye"
    assert rig.client.consolidated[0]["session_id"] == old
    assert rig.client.turn_sessions[-1] == old              # the farewell is part of the session


def test_normal_turn_is_not_goodbye(rig):
    done = []
    rig.ctl.subscribe(lambda e: done.append(e) if e["type"] == "turn_done" else None)
    run_text(rig, "what is a goodbye in french")
    assert done[-1]["goodbye"] is False and not rig.client.consolidated


def _timed(rig, kind, seconds):
    """A fake UI animation call that logs (kind, name, start time) and lasts seconds."""
    return lambda name: rig.log.append((kind, name, time.monotonic())) or seconds


def _timed_speaker(rig, reply_s=0.0):
    orig = rig.spk.play

    def play(path, block=True):
        rig.log.append(("play_at", Path(path).name, time.monotonic()))
        if not Path(path).name.startswith("sfx-"):
            time.sleep(reply_s)          # the spoken reply takes a while
        return orig(path, block)
    rig.spk.play = play


def test_sound_plays_during_the_dance_not_after(rig):
    """"Can you dance?": dance, bounce, wiggle, victory. The victory sound starts with
    the dance, and the reply starts once the sound is done, over the animations."""
    from app.server.client import Action
    rig.ui.play_expression = _timed(rig, "expression", 0.3)
    _timed_speaker(rig, reply_s=1.0)
    rig.client.actions = (Action("expression", "dance"), Action("expression", "bounce"),
                          Action("expression", "wiggle"), Action("sound", "victory", b"WIN"))
    run_text(rig)
    anims = [e for e in rig.log if e[0] == "expression"]
    plays = [e for e in rig.log if e[0] == "play_at"]
    assert [e[1] for e in anims] == ["dance", "bounce", "wiggle"]
    assert plays[0][1] == "sfx-3.wav" and plays[1][1] != "sfx-3.wav"
    assert plays[0][2] < anims[1][2]               # victory starts while BMO dances
    assert plays[1][2] < anims[-1][2] + 0.3        # the reply does not wait for the dance
    assert rig.spk.played[0].read_bytes() == b"WIN"
    assert rig.spk.played[-1].read_bytes() == WAV


def test_reply_waits_for_the_sound_queue(rig):
    from app.server.client import Action
    _timed_speaker(rig)
    orig = rig.spk.play

    def slow(path, block=True):
        r = orig(path, block)
        if Path(path).name.startswith("sfx-"):
            time.sleep(0.2)
            rig.log.append(("sound_end", Path(path).name, time.monotonic()))
        return r
    rig.spk.play = slow
    rig.client.actions = (Action("sound", "coin", b"C1"), Action("sound", "boing", b"C2"))
    run_text(rig)
    seq = [e[:2] for e in rig.log if e[0] in ("play_at", "sound_end")]
    assert seq[:4] == [("play_at", "sfx-0.wav"), ("sound_end", "sfx-0.wav"),
                       ("play_at", "sfx-1.wav"), ("sound_end", "sfx-1.wav")]
    assert seq[4][0] == "play_at" and len(seq) == 5


def test_emotion_after_the_animations_finish(rig):
    from app.server.client import Action
    rig.ui.play_expression = _timed(rig, "expression", 0.2)
    rig.ui.set_emotion = lambda name: rig.log.append(("emotion", name, time.monotonic()))
    rig.client.actions = (Action("expression", "wink"), Action("expression", "nod"))
    rig.client.extra = {"emotion": "excited"}
    rig.spk.play = lambda path, block=True: time.sleep(0.6) or True
    run_text(rig)
    seq = [e for e in rig.log if e[0] in ("expression", "emotion")]
    assert [e[:2] for e in seq] == [("expression", "wink"), ("expression", "nod"),
                                    ("emotion", "excited")]
    assert seq[2][2] - seq[1][2] >= 0.19


def test_song_screen_after_the_animations(rig):
    from app.server.client import Action
    rig.ui.play_expression = _timed(rig, "expression", 0.2)
    rig.ui.set_overlay = lambda kind, lyrics=(): rig.log.append(("overlay", kind))
    rig.client.actions = (Action("expression", "bounce"),)
    rig.client.extra = {"song": "silly"}
    rig.spk.play = lambda path, block=True: time.sleep(0.5) or True
    run_text(rig)
    assert [e[:2] for e in rig.log if e[0] in ("expression", "overlay")] == [
        ("expression", "bounce"), ("overlay", "song")]


def test_animations_left_when_the_reply_ends_are_dropped(rig):
    from app.server.client import Action
    rig.ui.play_expression = _timed(rig, "expression", 0.4)
    rig.client.actions = tuple(Action("expression", n) for n in ("a", "b", "c", "d"))
    run_text(rig)          # the reply ends at once
    time.sleep(0.6)
    assert len([e for e in rig.log if e[0] == "expression"]) < 4


def test_face_show_plays_out_when_nothing_is_spoken(rig):
    from app.server.client import Action
    rig.cfg.ui.text_only = True
    rig.ui.play_expression = _timed(rig, "expression", 0.05)
    rig.ui.show_face = _timed(rig, "face", 0.05)
    rig.client.actions = (Action("expression", "wink"), Action("face", "surprised"),
                          Action("expression", "heart_eyes"))
    rig.client.calls.clear()
    rig.ctl.submit_text("show me your faces", speak=False)
    assert rig.ctl.wait_idle(5)
    assert [e[1] for e in rig.log if e[0] in ("expression", "face")] == [
        "wink", "surprised", "heart_eyes"]


def test_music_screen_and_looping_dance_at_music_start(rig):
    """"Dance and play some music": the track starts music_start_s into the reply audio;
    from then the music screen shows and the dance loops until the audio ends."""
    rig.ui.play_expression = _timed(rig, "expression", 0.1)
    rig.ui.set_overlay = lambda kind, lyrics=(): rig.log.append(("overlay", kind, time.monotonic()))
    rig.client.stream = [_act("music", "dance_party")]
    rig.client.extra = {"music_start_s": 0.3}

    def play(path, block=True):
        rig.log.append(("play_at", Path(path).name, time.monotonic()))
        time.sleep(0.8)
        rig.log.append(("play_end", Path(path).name, time.monotonic()))
        return True
    rig.spk.play = play
    run_text(rig)
    time.sleep(0.2)
    start = next(e[2] for e in rig.log if e[0] == "play_at")
    end = next(e[2] for e in rig.log if e[0] == "play_end")
    overlay = [e for e in rig.log if e[0] == "overlay"]
    dances = [e[2] for e in rig.log if e[0] == "expression" and e[1] == "dance"]
    assert len(overlay) == 1 and overlay[0][1] == "music"
    assert overlay[0][2] - start >= 0.29
    assert len(dances) >= 3 and dances[0] - start >= 0.29
    assert all(t <= end + 0.01 for t in dances)    # the loop stops with the audio


def test_action_sounds_muted_in_text_only(rig):
    from app.server.client import Action
    rig.cfg.ui.text_only = True
    rig.client.actions = (Action("sound", "coin", b"COIN"),)
    run_text(rig)
    assert rig.spk.played == []



def _act(kind, name, audio=None):
    from app.server.client import Action
    return {"type": "action", "action": Action(kind, name, audio)}


def test_stream_status_shows_while_thinking(rig):
    events, calls = [], []
    rig.ctl.subscribe(events.append)
    rig.ui.set_caption = lambda text: calls.append(("caption", text))
    rig.ui.play_expression = lambda name: calls.append(("expression", name)) or 0.0
    rig.client.stream = [{"type": "status", "status": "searching_memories",
                          "text": "Searching memories...", "expression": "look_around"}]
    run_text(rig)
    assert calls == [("caption", "Searching memories..."), ("expression", "look_around")]
    status = [e for e in events if e["type"] == "status"]
    assert status and status[0]["text"] == "Searching memories..." and status[0]["turn"]
    assert any(e["type"] == "phase" and e["message"] == "Searching memories..." for e in events)


def test_streamed_actions_play_once_in_two_queues(rig):
    rig.ui.play_expression = lambda name: rig.log.append(("expression", name)) or 0.05
    rig.ui.show_face = lambda name: rig.log.append(("face", name)) or 0.05
    rig.ui.set_emotion = lambda name: rig.log.append(("emotion", name))
    rig.spk.play = lambda path, block=True: (rig.log.append(("play", Path(path).name)),
                                             time.sleep(0.4))
    rig.client.extra = {"emotion": "happy"}
    rig.client.stream = [_act("expression", "wink"), _act("face", "surprised"),
                         _act("sound", "coin", b"COIN"), _act("expression", "heart_eyes")]
    run_text(rig)
    anims = [e for e in rig.log if e[0] in ("expression", "face", "emotion")]
    assert anims == [("expression", "wink"), ("face", "surprised"),
                     ("expression", "heart_eyes"), ("emotion", "happy")]
    plays = [e[1] for e in rig.log if e[0] == "play"]
    assert plays[0] == "sfx-2.wav" and len(plays) == 2      # the reply, nothing replayed
    assert rig.log.index(("play", "sfx-2.wav")) < rig.log.index(("expression", "heart_eyes"))


def test_expression_waits_for_its_animation(rig):
    rig.ui.play_expression = lambda name: 0.3
    rig.cfg.ui.text_only = True         # nothing spoken: the turn waits for the animation
    rig.client.stream = [_act("expression", "dance")]
    t0 = time.monotonic()
    rig.ctl.submit_text("dance", speak=False)
    assert rig.ctl.wait_idle(5)
    assert time.monotonic() - t0 >= 0.3


def test_interrupt_during_streamed_action_drops_the_reply(rig):
    started = threading.Event()

    def long_dance(name):
        started.set()
        return 5.0
    rig.ui.play_expression = long_dance
    rig.client.stream = [_act("expression", "dance"), _act("expression", "wink")]
    done = []
    rig.ctl.subscribe(lambda e: e["type"] == "turn_done" and done.append(e))
    rig.ctl.submit_text("show me")
    assert started.wait(5)
    rig.ctl.interrupt()
    assert rig.ctl.wait_idle(5)
    time.sleep(0.2)
    assert done and done[0]["ok"] is False
    assert rig.spk.played == []


def test_song_and_music_overlays(rig):
    calls = []
    rig.ui.set_overlay = lambda kind, lyrics=(): calls.append((kind, list(lyrics)))
    rig.client.stream = [{"type": "song", "mood": "silly"}]
    run_text(rig)
    assert calls == [("song", ["reply to req-1"])]
    rig.client.stream = [_act("music", "dance_party")]
    run_text(rig)
    assert calls[-1] == ("music", [])
    rig.client.stream = []
    run_text(rig)
    assert len(calls) == 2


def test_streamed_transcript_shown_once(rig):
    rig.client.stream = [{"type": "transcript", "text": "hi bmo"}]
    run_text(rig)
    assert rig.ui.texts.count(("user", "hi bmo")) == 1


def test_memory_changes_emit_a_memory_event(rig):
    events = []
    rig.ctl.subscribe(events.append)
    rig.client.extra = {"memory_changes": ({"op": "create", "name": "dog-finn"},
                                           {"op": "delete", "name": "old"})}
    run_text(rig)
    mem = [e for e in events if e["type"] == "memory"]
    assert mem and mem[0]["changes"] == [{"op": "create", "name": "dog-finn"}]


def test_save_memory(rig):
    from app.server.errors import BadResponse
    rig.client.put_memory = lambda n, t, c: {"name": n, "type": t, "content": c}
    assert rig.ctl.save_memory("dog", "pet", "Finn.")["saved"] is True

    def bad(n, t, c):
        raise BadResponse("unexpected status 422", status=422, detail="too long")
    rig.client.put_memory = bad
    assert rig.ctl.save_memory("dog", "pet", "x") == {"available": True, "saved": False,
                                                      "error": "too long"}
    rig.client.put_memory = lambda n, t, c: None
    assert rig.ctl.save_memory("dog", "pet", "x")["available"] is False


def _wav_piece(secs, amp):
    import io
    import wave
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(amp.to_bytes(2, "little", signed=True) * int(22050 * secs))
    return out.getvalue()


def _pcm_amps(stream):
    return [int.from_bytes(pcm[:2], "little", signed=True) for _, pcm in stream.writes]


def _chunks(*pieces, order=None):
    order = order if order is not None else range(len(pieces))
    return [{"type": "audio_chunk", "index": i, "audio": pieces[i]} for i in order]


def test_streamed_reply_plays_pieces_back_to_back_in_order(rig):
    """audio_stream: the pieces go, in index order, into one open speaker stream."""
    rig.cfg.audio_stream = True
    pieces = [_wav_piece(0.1, 100), _wav_piece(0.1, 200), _wav_piece(0.1, 300)]
    rig.client.stream = [{"type": "reply", "text": "reply to req-1", "emotion": "happy"},
                         *_chunks(*pieces, order=[1, 0, 2])]
    run_text(rig)
    assert rig.client.calls[0]["audio_stream"] is True
    [st] = rig.spk.streams
    assert ("stream", 22050) in rig.log and _pcm_amps(st) == [100, 200, 300] and st.closed
    assert not any(e[0] == "play" for e in rig.log)     # no second, whole-file playback
    assert rig.ui.texts.count(("bmo", "reply to req-1")) == 1
    assert BotState.SPEAKING in rig.ui.states and rig.ui.states[-1] == BotState.IDLE


def test_streamed_reply_waits_for_the_sound_queue(rig):
    from app.server.client import Action
    sound_end = []

    def play(path, block=True):
        time.sleep(0.3)
        sound_end.append(time.monotonic())
        return True
    rig.spk.play = play
    rig.client.stream = [{"type": "action", "action": Action("sound", "coin", b"COIN")},
                         {"type": "reply", "text": "hi", "emotion": "happy"},
                         *_chunks(_wav_piece(0.1, 100))]
    run_text(rig)
    [st] = rig.spk.streams
    assert st.writes[0][0] >= sound_end[0]


def test_streamed_song_times_each_lyric_line_to_its_piece(rig):
    """A song comes as an opening bar, one piece per sung line, then an outro bar."""
    rig.ui.set_overlay = lambda kind, lyrics=(): rig.log.append(("overlay", kind, list(lyrics)))
    rig.ui.time_lyrics = lambda: rig.log.append(("time_lyrics",))
    rig.ui.add_lyric_start = lambda s: rig.log.append(("lyric_at", round(s, 3)))
    pieces = [_wav_piece(0.1, 1), _wav_piece(0.2, 2), _wav_piece(0.2, 3), _wav_piece(0.1, 4)]
    rig.client.stream = [{"type": "reply", "text": "La la\nFinn", "emotion": "happy",
                          "song": "silly"}, *_chunks(*pieces)]
    run_text(rig)
    assert ("overlay", "song", ["La la", "Finn"]) in rig.log
    assert ("time_lyrics",) in rig.log
    assert [e[1] for e in rig.log if e[0] == "lyric_at"] == [0.1, 0.3, 0.5]


def test_streamed_music_screen_at_music_start(rig):
    rig.ui.play_expression = _timed(rig, "expression", 0.1)
    rig.ui.set_overlay = lambda kind, lyrics=(): rig.log.append(("overlay", kind, time.monotonic()))
    rig.spk.stream_play_s = 0.8
    rig.client.stream = [_act("music", "dance_party"),
                         {"type": "reply", "text": "hi", "emotion": "happy"},
                         *_chunks(_wav_piece(0.1, 1), _wav_piece(0.1, 2))]
    rig.client.extra = {"music_start_s": 0.3}
    run_text(rig)
    time.sleep(0.2)
    [st] = rig.spk.streams
    start = st.writes[0][0]
    end = next(e[1] for e in rig.log if e[0] == "stream_end")
    overlay = [e for e in rig.log if e[0] == "overlay"]
    dances = [e[2] for e in rig.log if e[0] == "expression" and e[1] == "dance"]
    assert len(overlay) == 1 and overlay[0][1] == "music" and overlay[0][2] - start >= 0.29
    assert dances and all(start + 0.29 <= t <= end + 0.01 for t in dances)


def test_interrupt_stops_a_streamed_reply(rig):
    rig.spk.stream_play_s = None        # plays until stopped
    rig.client.stream = [{"type": "reply", "text": "a long story", "emotion": "happy"},
                         *_chunks(_wav_piece(0.1, 1))]
    rig.ctl.submit_text("tell me a story")
    deadline = time.monotonic() + 5
    while not (rig.spk.streams and rig.spk.streams[0].closed) and time.monotonic() < deadline:
        time.sleep(0.01)
    rig.ctl.interrupt()
    assert rig.ctl.wait_idle(2)
    assert ("speaker_stop",) in rig.log


def test_no_audio_stream_when_the_pi_does_not_play_it(rig):
    rig.cfg.audio_stream = True
    rig.ctl.submit_text("hi", speak=True, play_on_pi=False)
    assert rig.client.entered.wait(5) and rig.ctl.wait_idle(5)
    assert "audio_stream" not in rig.client.calls[0]
    rig.cfg.audio_stream = False
    rig.client.entered.clear()
    run_text(rig)
    assert "audio_stream" not in rig.client.calls[1]


def test_audio_stream_is_off_by_default_and_each_turn_logs_its_mode(rig, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="app.controller")
    run_text(rig)
    assert "audio_stream" not in rig.client.calls[0]
    assert "Turn audio: asked=whole got=whole" in caplog.text
    assert "Reply audio starts (audio=whole)" in caplog.text
    rig.cfg.audio_stream = True
    rig.client.stream = [{"type": "reply", "text": "hi", "emotion": "happy"},
                         *_chunks(_wav_piece(0.1, 1))]
    rig.client.extra = {"audio_streamed": True}
    run_text(rig)
    assert "Turn audio: asked=stream got=stream" in caplog.text
    assert "Reply audio starts (audio=stream)" in caplog.text


def test_interrupt_while_speaking_marks_turn_done_interrupted(rig):
    rig.spk.block = True
    events = collect(rig.ctl)
    rig.ctl.submit_text("hi")
    assert rig.spk.playing.wait(5)
    rig.ctl.interrupt()
    _join_turns()
    done = of(events, "turn_done")
    assert len(done) == 1
    assert done[0]["interrupted"] is True and done[0]["spoke_on_pi"] is True
    assert done[0]["ok"] is False


def test_preempt_while_speaking_is_not_marked_interrupted(rig):
    rig.spk.block = True
    events = collect(rig.ctl)
    rig.ctl.submit_text("hi")
    assert rig.spk.playing.wait(5)
    rig.spk.block = None
    rig.ctl.submit_text("something else")
    rig.ctl.wait_idle(5)
    _join_turns()
    first = of(events, "turn_done")[0]
    assert first["interrupted"] is False and first["spoke_on_pi"] is False


def test_interrupt_while_thinking_is_not_marked_interrupted(rig):
    rig.client.block = threading.Event()
    events = collect(rig.ctl)
    rig.ctl.submit_text("hi")
    assert rig.client.entered.wait(5)
    rig.ctl.interrupt()
    rig.client.block.set()
    _join_turns()
    assert of(events, "turn_done")[0]["interrupted"] is False

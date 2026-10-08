import threading
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

    def health(self):
        return {"ok": True}

    def new_request_id(self):
        self.n += 1
        return f"req-{self.n}"

    def interact(self, **kw):
        self.calls.append(kw)
        self.log.append(("interact", kw["request_id"]))
        self.entered.set()
        if self.block is not None:
            self.block.wait(5)
        if self.error:
            raise self.error
        return SimpleNamespace(transcript="hi bmo", text=f"reply to {kw['request_id']}",
                               audio_wav=WAV, request_id=kw["request_id"],
                               raw=self.raw)

    def forget_memories(self):
        self.log.append(("forget_memories",))
        return 3

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

    def play(self, path, block=True):
        self.played.append(Path(path))
        self.log.append(("play", Path(path).name))
        self._stop.clear()
        self.playing.set()
        if self.block is not None:
            self._stop.wait(5)
            return False
        return True

    def configure(self):
        self.log.append(("speaker_configure",))

    def play_effect(self, category):
        self.log.append(("effect", category))

    def stop(self):
        self.log.append(("speaker_stop",))
        self._stop.set()


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
    assert rig.ctl.delete_memory(1) == {"available": True, "deleted": False}
    rig.client.delete_memory = lambda i: None
    assert rig.ctl.delete_memory(1) == {"available": False, "deleted": False}
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

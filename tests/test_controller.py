import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Config
from app.controller import InteractionController
from app.hardware.camera import CameraError
from app.hardware.input import Action
from app.server.errors import AuthError
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

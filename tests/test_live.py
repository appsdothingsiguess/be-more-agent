import numpy as np

from app.audio.capture import FRAME_SAMPLES
from app.audio.wakeword import WakeUnavailable
from app.config import Config
from app.live import LiveListener, pretty_name
from app.ui.states import BotState

FRAME = np.zeros(FRAME_SAMPLES, dtype=np.int16)
DT = 0.08


class FakeCapture:
    frame_s = DT
    error = None
    on_recover = None

    def add_sink(self, fn):
        pass

    def remove_sink(self, fn):
        pass


class FakeController:
    def __init__(self):
        self.state = BotState.IDLE
        self.listening_source = None
        self.idle_hint = ""
        self.calls, self.events, self.subs = [], [], []
        self.speaker = type("S", (), {"is_playing": False})()

    def subscribe(self, fn):
        self.subs.append(fn)
        return lambda: self.subs.remove(fn)

    def publish(self, ev):
        self.events.append(ev)
        for fn in list(self.subs):
            fn(ev)

    def start_listening(self, source="button"):
        self.calls.append(("start", source))
        if self.state not in (BotState.IDLE, BotState.ERROR):
            return False
        self.state, self.listening_source = BotState.LISTENING, source
        return True

    def finish_listening(self):
        self.calls.append(("finish",))
        self.state, self.listening_source = BotState.THINKING, None

    def cancel_listening(self, quiet=False):
        self.calls.append(("cancel", quiet))
        self.state, self.listening_source = BotState.IDLE, None

    def prewarm(self):
        self.calls.append(("prewarm",))

    def lives(self):
        return [e for e in self.events if e["type"] == "live"]


class FakeDetector:
    name = "hey_jarvis"

    def __init__(self):
        self.next_score, self.resets = 0.0, 0

    def score(self, frame):
        return self.next_score

    def reset(self):
        self.resets += 1


class FakeVAD:
    def __init__(self):
        self.speech = False
        self.resets = 0

    def is_speech(self, frame):
        return self.speech

    def reset(self):
        self.resets += 1


class Rig:
    def __init__(self, wake=True, detector_error=None):
        self.cfg = Config()
        self.cfg.wake_word.enabled = wake
        self.cfg.listen.end_silence_seconds = 0.24
        self.cfg.listen.no_speech_timeout = 0.4
        self.cfg.listen.followup_seconds = 0.4
        self.cfg.listen.mute_tail_seconds = 0.2
        self.cfg.wake_word.cooldown_seconds = 1.0
        self.ctl = FakeController()
        self.det, self.vad = FakeDetector(), FakeVAD()
        self.now = 100.0

        def factory():
            if detector_error:
                raise detector_error
            return self.det
        self.live = LiveListener(self.cfg, self.ctl, FakeCapture(), factory, lambda: self.vad,
                                 clock=lambda: self.now)
        self.ctl.subscribe(self.live._on_event)

    def step(self, n=1, speech=None, score=None):
        if speech is not None:
            self.vad.speech = speech
        if score is not None:
            self.det.next_score = score
        for _ in range(n):
            self.now += DT
            self.live.step(FRAME)

    def calls(self):
        return self.ctl.calls


def test_pretty_name():
    assert pretty_name("hey_jarvis") == "Hey Jarvis"
    assert pretty_name("hey_bmo") == "Hey BMO"


def test_arms_publishes_and_sets_hint():
    r = Rig()
    r.step()
    assert r.ctl.lives()[-1] == {"type": "live", "armed": True, "model": "hey_jarvis",
                                 "error": None, "followup": True}
    assert r.ctl.idle_hint == "Say 'Hey Jarvis'"


def test_wake_then_end_of_speech_finishes():
    r = Rig()
    r.step(score=0.9)
    assert r.calls() == [("start", "wake"), ("prewarm",)]
    assert r.live.capture.skip_preroll  # the clip should not start with "hey jarvis"
    r.step(3, score=0.0, speech=True)
    r.step(2, speech=False)
    assert ("finish",) not in r.calls()
    r.step(1)
    assert r.calls()[-1] == ("finish",)


def test_wake_cooldown_and_no_prewarm_flag():
    r = Rig()
    r.cfg.listen.prewarm_on_wake = False
    r.step(score=0.9)
    assert r.calls() == [("start", "wake")]
    r.ctl.state = BotState.IDLE  # e.g. cancelled
    r.live._mode = "armed"
    r.step(score=0.9)
    assert r.calls().count(("start", "wake")) == 1  # cooldown


def test_no_speech_timeout_cancels_quietly():
    r = Rig()
    r.step(score=0.9)
    r.step(6, score=0.0, speech=False)
    assert ("cancel", True) in r.calls()
    assert r.ctl.state is BotState.IDLE


def test_no_detection_while_busy():
    r = Rig()
    for st in (BotState.SPEAKING, BotState.THINKING, BotState.CAPTURING):
        r.ctl.state = st
        r.step(3, score=0.99)
    assert r.calls() == []
    r.ctl.state = BotState.IDLE
    r.ctl.speaker.is_playing = True
    r.step(3, score=0.99)
    assert r.calls() == []


def test_mute_tail_then_detector_reset():
    r = Rig()
    r.ctl.state = BotState.SPEAKING
    r.step()
    r.ctl.state = BotState.IDLE
    resets = r.det.resets
    r.step(score=0.99)  # inside tail
    assert r.calls() == [] and r.det.resets == resets
    r.step(3, score=0.99)  # tail over: detector reset, then it may fire
    assert r.det.resets == resets + 1


def done(**kw):
    return {"type": "turn_done", "turn": "t", "source": "wake", "client_id": None,
            "ok": True, "spoke_on_pi": True, **kw}


def test_followup_opens_and_times_out_quietly():
    r = Rig()
    r.ctl.state = BotState.SPEAKING
    r.step()
    r.ctl.state = BotState.IDLE
    r.ctl.publish(done())
    r.step(1)
    assert ("start", "followup") not in r.calls()  # waiting out the tail
    r.step(3)
    assert ("start", "followup") in r.calls()
    r.step(6, speech=False)
    assert r.calls()[-1] == ("cancel", True)
    r.step(2)
    assert r.calls().count(("start", "followup")) == 1  # not re-opened


def test_followup_speech_is_sent():
    r = Rig()
    r.ctl.publish(done())
    r.step(5)
    r.step(2, speech=True)
    r.step(4, speech=False)
    assert r.calls()[-1] == ("finish",)


def test_no_followup_for_web_failed_silent_or_disabled():
    for ev, edit in [(done(source="web"), None), (done(ok=False), None),
                     (done(spoke_on_pi=False), None), (done(), "off")]:
        r = Rig()
        if edit:
            r.cfg.listen.followup = False
        r.ctl.publish(ev)
        r.step(8)
        assert not [c for c in r.calls() if c[0] == "start"], ev


def test_followup_cancelled_by_new_busy_turn():
    r = Rig()
    r.ctl.publish(done())
    r.step(1)
    r.ctl.state = BotState.THINKING  # user started something else
    r.step(1)
    r.ctl.state = BotState.IDLE
    r.step(8)
    assert not [c for c in r.calls() if c[0] == "start"]


def test_setting_toggle_arms_and_disarms():
    r = Rig(wake=False)
    r.step()
    assert r.ctl.lives()[-1]["armed"] is False
    assert r.ctl.idle_hint == "Press Start to talk"
    r.step(score=0.99)
    assert r.calls() == []
    r.cfg.wake_word.enabled = True
    r.ctl.publish({"type": "setting", "key": "wake_word.enabled", "value": True})
    r.step(score=0.0)
    assert r.ctl.lives()[-1]["armed"] is True
    r.cfg.wake_word.enabled = False
    r.ctl.publish({"type": "setting", "key": "wake_word.enabled", "value": False})
    r.step(score=0.99)
    assert r.ctl.lives()[-1]["armed"] is False and r.calls() == []
    r.cfg.listen.followup = False
    r.ctl.publish({"type": "setting", "key": "listen.followup", "value": False})
    r.step()
    assert r.ctl.lives()[-1]["followup"] is False


def test_wake_unavailable_publishes_error_and_button_autostop_works():
    r = Rig(detector_error=WakeUnavailable("no models"))
    r.step()
    last = r.ctl.lives()[-1]
    assert last["armed"] is False and "no models" in last["error"]
    assert r.ctl.idle_hint == "Press Start to talk"
    r.ctl.start_listening("button")  # user presses Start
    r.step(7, speech=False)
    assert r.calls()[-1] == ("cancel", True)  # no speech at all
    r.ctl.state = BotState.IDLE
    r.ctl.start_listening("button")
    r.step(2, speech=True)
    r.step(4, speech=False)
    assert r.calls()[-1] == ("finish",)


def test_button_autostop_can_be_disabled():
    r = Rig(wake=False)
    r.cfg.listen.auto_stop_button = False
    r.ctl.start_listening("button")
    r.step(2, speech=True)
    r.step(20, speech=False)
    assert ("finish",) not in r.calls()


def test_mic_error_disarms_and_recovery_republishes():
    r = Rig()
    r.step()
    r.live.capture.error = "Microphone unavailable"
    r.live._on_recover()
    r.step()
    assert r.ctl.lives()[-1]["armed"] is False
    r.live.capture.error = None
    r.live._on_recover()
    r.step()
    assert r.ctl.lives()[-1]["armed"] is True


def test_step_exceptions_do_not_kill_worker():
    r = Rig()
    r.det.score = lambda f: 1 / 0
    r.live._frames.put(FRAME)
    r.live._stop.clear()
    try:
        r.live.step(FRAME)
    except ZeroDivisionError:
        pass
    # _run wraps step(); verify a full run survives
    import threading
    r.live._thread = threading.Thread(target=r.live._run, daemon=True)
    r.live._thread.start()
    r.live._frames.put(FRAME)
    threading.Event().wait(0.1)
    assert r.live._thread.is_alive()
    r.live._stop.set()
    r.live._thread.join(1)


def test_queue_drops_oldest():
    r = Rig()
    for i in range(40):
        r.live._on_frame(np.full(FRAME_SAMPLES, i, dtype=np.int16))
    assert r.live._frames.qsize() == 25
    assert r.live._frames.get_nowait()[0] == 15

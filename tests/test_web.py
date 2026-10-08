import io
import logging
import queue
import stat
import threading
import time
import wave
from collections import deque
from types import SimpleNamespace

import pytest
import requests

from app.config import Config
from app.settings import SETTINGS, SettingError, set_setting
from app.web.server import WebServer, load_or_create_pin

PIN = "123456"


class FakeController:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = SimpleNamespace(value="idle")
        self.history = deque([{"type": "text", "who": "user", "text": "hi", "time": 1.0}])
        self.messages = []
        self.applied = []
        self.interrupts = 0
        self.subs = []
        self.audio_calls = []
        self.audio_seen = []
        self.audio_result = "a1b2c3d4e5f60718"
        self.text_calls = []
        self.replies = {}
        self.sticky = [{"type": "phase", "phase": "idle", "turn": None, "message": "ready"},
                       {"type": "live", "armed": True, "model": "hey_jarvis", "error": None}]

    def snapshot(self):
        return list(self.sticky)

    def reply_path(self, turn_id):
        return self.replies.get(turn_id)

    def submit_audio(self, path, *, speak=None, play_on_pi=None, source="web", client_id=None):
        import os
        self.audio_seen.append(os.path.exists(path) and oct(os.stat(path).st_mode & 0o777))
        self.audio_calls.append((path, speak, play_on_pi, source, client_id))
        return self.audio_result

    def subscribe(self, fn):
        self.subs.append(fn)
        return lambda: self.subs.remove(fn) if fn in self.subs else None

    def publish(self, ev):
        for fn in list(self.subs):
            fn(ev)

    def submit_text(self, text, speak=None, *, play_on_pi=None, source="web", client_id=None):
        self.messages.append((text, speak))
        self.text_calls.append((text, speak, play_on_pi, source, client_id))
        return "0123456789abcdef"

    def interrupt(self):
        self.interrupts += 1

    def apply_setting(self, key, value):
        self.applied.append((key, value))
        return set_setting(self.cfg, key, value)

    def server_status(self):
        return {"reachable": True, "bmo_ready": True, "mode": "bmo"}


@pytest.fixture
def web(tmp_path):
    cfg = Config()
    cfg.web.host = "127.0.0.1"
    cfg.web.port = 0
    cfg.runtime_dir = str(tmp_path / "rt")
    cfg.web.trusted_proxies = ["10.9.9.9"]
    cfg.web.public_origins = ["https://bmo.example.com"]
    ctrl = FakeController(cfg)
    srv = WebServer(cfg, ctrl, pin=PIN)
    srv.start()
    srv.ctrl = ctrl
    assert srv.cfg is cfg
    srv.base = f"http://127.0.0.1:{srv.port}"
    yield srv
    srv.stop()


@pytest.fixture
def sess(web):
    s = requests.Session()
    r = s.post(web.base + "/api/login", json={"pin": PIN})
    assert r.status_code == 200
    return s


def test_page_public(web):
    r = requests.get(web.base + "/")
    assert r.status_code == 200 and "BMO" in r.text
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_api_requires_login(web):
    for p in ("/api/state", "/api/settings", "/api/status", "/api/events"):
        assert requests.get(web.base + p).status_code == 401
    assert requests.post(web.base + "/api/message", json={"text": "x"}).status_code == 401


def test_wrong_pin_and_lockout(web):
    for _ in range(5):
        assert requests.post(web.base + "/api/login", json={"pin": "000000"}).status_code == 401
    assert requests.post(web.base + "/api/login", json={"pin": PIN}).status_code == 429


def test_login_cookie(web):
    r = requests.post(web.base + "/api/login", json={"pin": PIN})
    c = r.headers["Set-Cookie"]
    assert c.startswith("bmo_session=") and "HttpOnly" in c and "SameSite=Strict" in c
    assert PIN not in r.text


def test_state(web, sess):
    d = sess.get(web.base + "/api/state").json()
    assert d["state"] == "idle" and "speaker.volume" in d["settings"] and "text_only" in d


def test_message(web, sess):
    r = sess.post(web.base + "/api/message", json={"text": " hello ", "speak": False})
    assert r.status_code == 202
    assert web.ctrl.messages == [("hello", False)]
    sess.post(web.base + "/api/message", json={"text": "yo"})
    assert web.ctrl.messages[-1] == ("yo", None)


def test_message_limits(web, sess):
    assert sess.post(web.base + "/api/message", json={"text": "x" * 2001}).status_code == 413
    assert sess.post(web.base + "/api/message", json={"text": "  "}).status_code == 400
    r = sess.post(web.base + "/api/message", data=b"x" * 20000,
                  headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = sess.post(web.base + "/api/message", data="text=hi",
                  headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert web.ctrl.messages == []


def test_settings(web, sess):
    r = sess.post(web.base + "/api/settings", json={"bogus": 1})
    assert r.status_code == 400 and r.json()["key"] == "bogus"
    r = sess.post(web.base + "/api/settings",
                  json={"ui.text_only": True, "speaker.volume": 500})
    assert r.status_code == 400 and r.json()["key"] == "speaker.volume"
    assert web.ctrl.applied == [] and web.cfg.ui.text_only is False
    r = sess.post(web.base + "/api/settings",
                  json={"ui.text_only": True, "speaker.volume": 40})
    assert r.status_code == 200
    assert r.json()["ui.text_only"] is True and r.json()["speaker.volume"] == "40%"
    assert len(web.ctrl.applied) == 2
    assert sess.get(web.base + "/api/settings").json()["speaker.volume"] == "40%"


def test_status_and_interrupt(web, sess):
    d = sess.get(web.base + "/api/status").json()
    assert d["bmo_ready"] is True and d["bmo_state"] == "idle"
    sess.post(web.base + "/api/interrupt", json={})
    assert web.ctrl.interrupts == 0
    web.ctrl.state = SimpleNamespace(value="thinking")
    assert sess.post(web.base + "/api/interrupt", json={}).json() == {"ok": True}
    assert web.ctrl.interrupts == 1


def test_sse(web, sess):
    web.ctrl.sticky = [{"type": "state", "state": "idle", "message": ""}]
    with sess.get(web.base + "/api/events", stream=True, timeout=5) as r:
        assert r.headers["Content-Type"].startswith("text/event-stream")
        lines = r.iter_lines(chunk_size=1, decode_unicode=True)
        got = []

        def read(n):
            while len(got) < n:
                line = next(lines)
                if line.startswith("data: "):
                    got.append(line[6:])

        read(2)
        assert '"state"' in got[0] and '"idle"' in got[0]
        assert '"hi"' in got[1]
        for _ in range(50):
            if web.ctrl.subs:
                break
            time.sleep(0.02)
        web.ctrl.publish({"type": "text", "who": "bmo", "text": "live!", "time": 2.0})
        read(3)
        assert "live!" in got[2]
    for _ in range(100):
        if not web.ctrl.subs:
            break
        time.sleep(0.05)


def test_static_traversal(web):
    for p in ("/static/../server.py", "/static/..%2fserver.py", "/static/%2e%2e/server.py",
              "/static/nope.txt"):
        r = requests.get(web.base + p)
        assert r.status_code == 404, p
    assert requests.get(web.base + "/static/index.html").status_code == 200


def test_pin_file(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    p = tmp_path / "cfg" / "pin"
    pin = load_or_create_pin(p)
    assert len(pin) == 6 and pin.isdigit()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert load_or_create_pin(p) == pin
    assert pin not in caplog.text and str(p) in caplog.text


class MemController(FakeController):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.forgets = 0
        self.deleted = []

    def forget_memory(self):
        self.forgets += 1
        return {"local_cleared": True, "server": "ok", "forgotten": 2}

    def list_memories(self):
        return {"available": True, "error": None, "memories": [
            {"id": 1, "content": "likes tea", "type": "fact", "created_at": "x"}]}

    def delete_memory(self, memory_id):
        self.deleted.append(memory_id)
        return {"available": True, "deleted": True}

    def conversation(self):
        return [{"role": "user", "content": "hi"}]


@pytest.fixture
def memweb(web):
    web.controller = web.ctrl = MemController(web.cfg)
    return web


MEM_ROUTES = (("get", "/api/memories"), ("post", "/api/memories/forget"),
              ("post", "/api/memories/delete"))


def test_memory_requires_login(memweb):
    for method, p in MEM_ROUTES:
        r = getattr(requests, method)(memweb.base + p, **({"json": {"id": 1}} if method == "post" else {}))
        assert r.status_code == 401, p
    assert memweb.ctrl.forgets == 0 and memweb.ctrl.deleted == []


def test_memory_get(memweb, sess):
    d = sess.get(memweb.base + "/api/memories").json()
    assert d["long_term"]["memories"][0]["content"] == "likes tea"
    assert d["conversation"] == [{"role": "user", "content": "hi"}]


def test_memory_forget(memweb, sess):
    r = sess.post(memweb.base + "/api/memories/forget", json={})
    assert r.status_code == 200
    assert r.json() == {"local_cleared": True, "server": "ok", "forgotten": 2}
    assert memweb.ctrl.forgets == 1


def test_memory_delete_validation(memweb, sess):
    for body in ({}, {"id": "3"}, {"id": True}, {"id": 0}, {"id": -1}, {"id": 1.5}):
        r = sess.post(memweb.base + "/api/memories/delete", json=body)
        assert r.status_code == 400, body
    assert memweb.ctrl.deleted == []
    r = sess.post(memweb.base + "/api/memories/delete", json={"id": 7})
    assert r.status_code == 200 and r.json() == {"available": True, "deleted": True}
    assert memweb.ctrl.deleted == [7]


def test_memory_unsupported(web, sess):
    assert sess.get(web.base + "/api/memories").status_code == 501
    for p, body in (("forget", {}), ("delete", {"id": 1})):
        r = sess.post(web.base + "/api/memories/" + p, json=body)
        assert r.status_code == 501 and r.json() == {"error": "memory not supported"}


def test_page_has_memory_section(web):
    html = requests.get(web.base + "/").text
    assert 'id="memoryPanel"' in html and 'id="forget"' in html
    assert "Forget everything" in html


# -- W2 additions ---------------------------------------------------------
def wav(rate=16000, secs=1.0, width=2, ch=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\0" * int(rate * secs) * width * ch)
    return buf.getvalue()


def post_voice(sess, web, body, ctype="audio/wav", q=""):
    return sess.post(web.base + "/api/voice" + q, data=body, headers={"Content-Type": ctype})


def test_voice_happy(web, sess):
    r = post_voice(sess, web, wav(), q="?speak=1&pi=0&client=abcdEFGH12")
    assert r.status_code == 202 and r.json() == {"turn": "a1b2c3d4e5f60718"}
    path, speak, pi, source, client = web.ctrl.audio_calls[0]
    assert (speak, pi, source, client) == (True, False, "web", "abcdEFGH12")
    assert web.ctrl.audio_seen == ["0o600"]
    assert path.parent == web.cfg.runtime_path / "uploads"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_voice_defaults_and_alt_type(web, sess):
    assert post_voice(sess, web, wav(), "audio/x-wav").status_code == 202
    assert web.ctrl.audio_calls[0][1:] == (None, None, "web", None)


def test_voice_unauth(web):
    r = requests.post(web.base + "/api/voice", data=wav(), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 401 and not web.ctrl.audio_calls


def test_voice_too_large_and_type(web, sess):
    web.cfg.web.max_voice_bytes = 1000
    assert post_voice(sess, web, wav()).status_code == 413
    web.cfg.web.max_voice_bytes = 3_000_000
    time.sleep(1.1)
    assert post_voice(sess, web, wav(), "text/plain").status_code == 415
    assert not web.ctrl.audio_calls


@pytest.mark.parametrize("kw", [dict(ch=2), dict(width=3), dict(secs=0.1), dict(secs=31),
                                dict(rate=4000)])
def test_voice_rejects_bad_wav(web, sess, kw):
    assert post_voice(sess, web, wav(**kw)).status_code == 400
    assert not web.ctrl.audio_calls


def test_voice_garbage_and_none(web, sess):
    assert post_voice(sess, web, b"not a wav").status_code == 400
    time.sleep(1.1)
    web.ctrl.audio_result = None
    assert post_voice(sess, web, wav()).status_code == 503


def test_voice_rate_limit(web, sess):
    assert post_voice(sess, web, wav()).status_code == 202
    assert post_voice(sess, web, wav()).status_code == 429
    time.sleep(1.1)
    assert post_voice(sess, web, wav()).status_code == 202


def test_audio_route(web, sess):
    f = web.cfg.runtime_path / "r.wav"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(wav())
    web.ctrl.replies["00112233445566ff"] = f
    r = sess.get(web.base + "/api/audio/00112233445566ff.wav")
    assert r.status_code == 200 and r.content == wav()
    assert r.headers["Content-Type"] == "audio/wav" and r.headers["Cache-Control"] == "no-store"
    for bad in ("0011.wav", "00112233445566fe.wav", "..%2f..%2fetc%2fpasswd", "00112233445566FF.wav",
                "../00112233445566ff.wav"):
        assert sess.get(web.base + "/api/audio/" + bad).status_code == 404
    assert requests.get(web.base + "/api/audio/00112233445566ff.wav").status_code == 401


def test_security_headers(web):
    r = requests.get(web.base + "/")
    csp = r.headers["Content-Security-Policy"]
    assert "media-src 'self' blob:" in csp and "connect-src 'self'" in csp
    assert "worker-src 'self'" in csp
    assert r.headers["Permissions-Policy"] == "microphone=(self), camera=()"
    assert r.headers["Referrer-Policy"] == "same-origin"
    js = requests.get(web.base + "/static/app.js")
    if js.status_code == 200:
        assert js.headers["Content-Type"].startswith("application/javascript")


def test_sse_snapshot_first_and_status_live(web, sess):
    with sess.get(web.base + "/api/events", stream=True, timeout=5) as r:
        lines = r.iter_lines(chunk_size=1)
        first = next(l for l in lines if l.startswith(b"data:"))
        assert b'"phase"' in first
    live = sess.get(web.base + "/api/status").json()["live"]
    assert live["type"] == "live" and live["armed"] is True


def test_sse_cap(web, sess):
    streams = [sess.get(web.base + "/api/events", stream=True, timeout=5) for _ in range(8)]
    try:
        assert all(s.status_code == 200 for s in streams)
        assert sess.get(web.base + "/api/events", timeout=5).status_code == 503
    finally:
        for s in streams:
            s.close()


def test_message_turn_and_flags(web, sess):
    r = sess.post(web.base + "/api/message",
                  json={"text": "hi", "play_on_pi": False, "client_id": "abcdEFGH12"})
    assert r.status_code == 202 and r.json() == {"ok": True, "turn": "0123456789abcdef"}
    assert web.ctrl.text_calls[-1] == ("hi", None, False, "web", "abcdEFGH12")
    assert sess.post(web.base + "/api/message",
                     json={"text": "hi", "client_id": "../x"}).status_code == 400
    assert sess.post(web.base + "/api/message",
                     json={"text": "hi", "play_on_pi": "yes"}).status_code == 400


def test_xff_untrusted_peer_ignored(web):
    h = {"X-Forwarded-For": "1.2.3.4", "CF-Connecting-IP": "5.6.7.8"}
    for i in range(5):
        requests.post(web.base + "/api/login", json={"pin": "0"}, headers=
                      {"X-Forwarded-For": f"1.2.3.{i}"})
    # all counted against the real peer, so now locked regardless of header
    r = requests.post(web.base + "/api/login", json={"pin": PIN}, headers=h)
    assert r.status_code == 429


def test_client_ip_trusted(web):
    web.cfg.web.trusted_proxies = ["127.0.0.1"]
    web._proxies = [__import__("ipaddress").ip_network("127.0.0.1")]
    for i in range(5):
        requests.post(web.base + "/api/login", json={"pin": "0"},
                      headers={"X-Forwarded-For": "9.9.9.9, 1.1.1.1, 127.0.0.1"})
    assert requests.post(web.base + "/api/login", json={"pin": PIN},
                         headers={"X-Forwarded-For": "9.9.9.9, 1.1.1.2"}).status_code == 200
    assert requests.post(web.base + "/api/login", json={"pin": PIN},
                         headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429
    assert requests.post(web.base + "/api/login", json={"pin": PIN},
                         headers={"CF-Connecting-IP": "7.7.7.7"}).status_code == 200


def test_global_lockout(web, caplog):
    web._proxies = [__import__("ipaddress").ip_network("127.0.0.1")]
    with caplog.at_level(logging.WARNING):
        for i in range(30):
            requests.post(web.base + "/api/login", json={"pin": "0"},
                          headers={"CF-Connecting-IP": f"8.8.{i // 250}.{i % 250 + 1}"})
    r = requests.post(web.base + "/api/login", json={"pin": PIN},
                      headers={"CF-Connecting-IP": "4.4.4.4"})
    assert r.status_code == 429
    assert any("Global login lockout" in m for m in caplog.messages)


def test_origin_check(web, sess):
    url = web.base + "/api/message"
    bad = sess.post(url, json={"text": "x"}, headers={"Origin": "http://evil.example"})
    assert bad.status_code == 403
    ok = sess.post(url, json={"text": "x"}, headers={"Origin": web.base})
    assert ok.status_code == 202
    ok = sess.post(url, json={"text": "x"}, headers={"Origin": "https://bmo.example.com"})
    assert ok.status_code == 202
    r = requests.post(web.base + "/api/login", json={"pin": PIN},
                      headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


# -- sessions -------------------------------------------------------------
class SessionController(FakeController):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.new_sessions = 0

    def new_session(self):
        self.new_sessions += 1
        return {"id": "abc", "reason": "new_session", "consolidated": "ok"}

    def session_info(self):
        return {"id": "abc", "started_at": 1.0, "messages": 4}


@pytest.fixture
def sesweb(web):
    web.ctrl = web.controller = SessionController(web.cfg)
    return web


def test_session_new_requires_login(sesweb):
    assert requests.post(sesweb.base + "/api/session/new", json={}).status_code == 401
    assert sesweb.ctrl.new_sessions == 0


def test_session_new_origin_checked(sesweb, sess):
    r = sess.post(sesweb.base + "/api/session/new", json={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and sesweb.ctrl.new_sessions == 0


def test_session_new_ok(sesweb, sess):
    r = sess.post(sesweb.base + "/api/session/new", json={}, headers={"Origin": sesweb.base})
    assert r.status_code == 200
    assert r.json() == {"id": "abc", "reason": "new_session", "consolidated": "ok"}
    assert sesweb.ctrl.new_sessions == 1


def test_session_in_state(sesweb, sess):
    assert sess.get(sesweb.base + "/api/state").json()["session"]["messages"] == 4


def test_session_new_unsupported(web, sess):
    assert sess.post(web.base + "/api/session/new", json={}).status_code == 501
    assert "session" not in sess.get(web.base + "/api/state").json()

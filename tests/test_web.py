import logging
import queue
import stat
import threading
import time
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

    def subscribe(self, fn):
        self.subs.append(fn)
        return lambda: self.subs.remove(fn) if fn in self.subs else None

    def publish(self, ev):
        for fn in list(self.subs):
            fn(ev)

    def submit_text(self, text, speak=None):
        self.messages.append((text, speak))

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

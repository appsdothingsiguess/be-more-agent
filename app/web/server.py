"""Local web interface: PIN login, SSE chat feed, settings and status JSON API."""
from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app import settings as settings_mod
from app.config import Config
from app.settings import SETTINGS, SettingError

log = logging.getLogger(__name__)

STATIC_DIR = (Path(__file__).parent / "static").resolve()
SESSION_TTL = 30 * 24 * 3600
MAX_BODY = 16 * 1024
MAX_TEXT = 2000
FAIL_LIMIT = 5
FAIL_WINDOW = 300.0
LOCKOUT = 300.0
PING_SECONDS = 15.0
COOKIE = "bmo_session"
PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:")
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "application/javascript; charset=utf-8", ".json": "application/json",
         ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


def load_or_create_pin(path: str | os.PathLike) -> str:
    p = Path(path).expanduser()
    try:
        pin = p.read_text().strip()
        if pin:
            return pin
    except FileNotFoundError:
        pass
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pin = f"{secrets.randbelow(10**6):06d}"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pin + "\n")
    os.chmod(p, 0o600)
    log.info("Created web PIN file at %s", p)
    return pin


class WebServer:
    def __init__(self, cfg: Config, controller, *, pin: str | None = None):
        self.cfg = cfg
        self.controller = controller
        if pin is None:
            pin = load_or_create_pin(cfg.web.pin_file)
        self._pin = pin
        self._sessions: dict[str, float] = {}
        self._failures: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.port = cfg.web.port

    # -- auth -------------------------------------------------------------
    def check_pin(self, ip: str, pin) -> int:
        """Return 200 on success, 401 on wrong PIN, 429 when locked out."""
        now = time.monotonic()
        with self._lock:
            if self._locked.get(ip, 0) > now:
                return 429
            ok = isinstance(pin, str) and hmac.compare_digest(
                pin.encode(), self._pin.encode())
            if ok:
                self._failures.pop(ip, None)
                return 200
            fails = [t for t in self._failures.get(ip, []) if now - t < FAIL_WINDOW]
            fails.append(now)
            self._failures[ip] = fails
            if len(fails) >= FAIL_LIMIT:
                self._locked[ip] = now + LOCKOUT
                self._failures.pop(ip, None)
            return 401

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = time.time()
            self._sessions = {k: v for k, v in self._sessions.items() if v > now}
            self._sessions[token] = now + SESSION_TTL
        return token

    def valid_session(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            exp = self._sessions.get(token)
            if exp is None:
                return False
            if exp <= time.time():
                del self._sessions[token]
                return False
            return True

    def end_session(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        server = self

        class Handler(_Handler):
            web = server

        self._stopping.clear()
        self._httpd = ThreadingHTTPServer((self.cfg.web.host, self.cfg.web.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="web-server", daemon=True)
        self._thread.start()
        log.info("Web interface on %s:%s", self.cfg.web.host, self.port)

    def stop(self) -> None:
        self._stopping.set()
        httpd, self._httpd = self._httpd, None
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None


class _Handler(BaseHTTPRequestHandler):
    web: WebServer
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            pass  # client went away mid-request

    def log_message(self, format, *args):  # noqa: A002
        log.debug("web: " + format, *args)

    # -- helpers ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None,
              api: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if api:
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json", extra, api=True)

    def _token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            morsel = SimpleCookie(raw).get(COOKIE)
        except Exception:
            return None
        return morsel.value if morsel else None

    def _authed(self) -> bool:
        if self.web.valid_session(self._token()):
            return True
        self._drain()
        self._json(401, {"error": "login required"})
        return False

    def _drain(self) -> None:
        """Discard a small unread request body so keep-alive stays in sync."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= MAX_BODY:
            self.rfile.read(length)
        elif length > MAX_BODY:
            self.close_connection = True

    def _body(self):
        """Parse a JSON object body; sends an error and returns None on failure."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            self._json(400, {"error": "bad Content-Length"})
            return None
        if length > MAX_BODY:
            self.close_connection = True
            self._json(413, {"error": "body too large"})
            return None
        raw = self.rfile.read(length) if length else b""
        if ctype != "application/json":
            self._json(415, {"error": "Content-Type must be application/json"})
            return None
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid JSON"})
            return None
        if not isinstance(data, dict):
            self._json(400, {"error": "JSON object expected"})
            return None
        return data

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(unquote(path[len("/static/"):]))
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._authed():
            return
        c = self.web.controller
        if path == "/api/state":
            return self._json(200, {"state": c.state.value,
                                    "settings": settings_mod.current(self.web.cfg),
                                    "text_only": self.web.cfg.ui.text_only})
        if path == "/api/events":
            return self._events()
        if path == "/api/settings":
            return self._json(200, settings_mod.current(self.web.cfg))
        if path == "/api/status":
            st = dict(c.server_status())
            st["bmo_state"] = c.state.value
            return self._json(200, st)
        if path == "/api/memories":
            return self._memories()
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/login":
            return self._login()
        if not path.startswith("/api/"):
            self._drain()
            return self._json(404, {"error": "not found"})
        if not self._authed():
            return
        if path == "/api/logout":
            self._drain()
            self.web.end_session(self._token())
            return self._json(200, {"ok": True}, {
                "Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
        data = self._body()
        if data is None:
            return
        if path == "/api/message":
            return self._message(data)
        if path == "/api/interrupt":
            if self.web.controller.state.value not in ("idle", "error"):
                self.web.controller.interrupt()
            return self._json(200, {"ok": True})
        if path == "/api/settings":
            return self._settings(data)
        if path == "/api/memories/forget":
            return self._memory_call("forget_memory")
        if path == "/api/memories/delete":
            return self._memory_delete(data)
        self._json(404, {"error": "not found"})

    def _static(self, rel: str) -> None:
        try:
            target = (STATIC_DIR / rel).resolve()
            target.relative_to(STATIC_DIR)
            if not target.is_file():
                raise FileNotFoundError
            body = target.read_bytes()
        except (ValueError, OSError):
            return self._json(404, {"error": "not found"})
        extra = {"Cache-Control": "no-cache"}
        if target.suffix == ".html":
            extra["Content-Security-Policy"] = PAGE_CSP
        self._send(200, body, TYPES.get(target.suffix, "application/octet-stream"), extra)

    def _login(self) -> None:
        data = self._body()
        if data is None:
            return
        code = self.web.check_pin(self.client_address[0], data.get("pin"))
        if code == 429:
            return self._json(429, {"error": "too many attempts, try again later"},
                              {"Retry-After": str(int(LOCKOUT))})
        if code != 200:
            return self._json(401, {"error": "wrong PIN"})
        token = self.web.new_session()
        self._json(200, {"ok": True}, {
            "Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; "
                          f"Max-Age={SESSION_TTL}"})

    def _message(self, data: dict) -> None:
        text = data.get("text")
        speak = data.get("speak")
        if not isinstance(text, str) or not text.strip():
            return self._json(400, {"error": "text required"})
        if len(text) > MAX_TEXT:
            return self._json(413, {"error": f"text longer than {MAX_TEXT} characters"})
        if speak is not None and not isinstance(speak, bool):
            return self._json(400, {"error": "speak must be true, false or null"})
        self.web.controller.submit_text(text.strip(), speak=speak)
        self._json(202, {"ok": True})

    def _memories(self) -> None:
        c = self.web.controller
        if not all(callable(getattr(c, n, None)) for n in ("list_memories", "conversation")):
            return self._json(501, {"error": "memory not supported"})
        self._json(200, {"long_term": c.list_memories(), "conversation": c.conversation()})

    def _memory_call(self, name: str, *args) -> None:
        fn = getattr(self.web.controller, name, None)
        if not callable(fn):
            return self._json(501, {"error": "memory not supported"})
        self._json(200, fn(*args))

    def _memory_delete(self, data: dict) -> None:
        mid = data.get("id")
        if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0:
            return self._json(400, {"error": "id must be a positive integer"})
        self._memory_call("delete_memory", mid)

    def _settings(self, data: dict) -> None:
        if not data:
            return self._json(400, {"error": "no settings given"})
        for key, value in data.items():
            try:
                if key not in SETTINGS:
                    raise SettingError(f"unknown setting: {key}")
                SETTINGS[key](value)
            except SettingError as e:
                return self._json(400, {"error": str(e), "key": key})
        for key, value in data.items():
            try:
                self.web.controller.apply_setting(key, value)
            except SettingError as e:
                return self._json(400, {"error": str(e), "key": key})
        self._json(200, settings_mod.current(self.web.cfg))

    def _events(self) -> None:
        c = self.web.controller
        q: queue.Queue = queue.Queue()
        unsub = c.subscribe(q.put)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self._sse({"type": "state", "state": c.state.value, "message": "",
                       "time": time.time()})
            for ev in list(c.history):
                self._sse(ev)
            while not self.web._stopping.is_set():
                try:
                    ev = q.get(timeout=PING_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse(ev)
        except OSError:
            pass
        finally:
            unsub()

    def _sse(self, ev: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
        self.wfile.flush()

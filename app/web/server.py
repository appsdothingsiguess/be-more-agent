"""Local web interface: PIN login, SSE chat feed, settings and status JSON API."""
from __future__ import annotations

import hmac
import io
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import ssl
import threading
import time
import uuid
import wave
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from app import settings as settings_mod
from app.config import Config
from app.settings import SETTINGS, SettingError
from app.web import tls as tls_mod

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
GLOBAL_FAIL_LIMIT = 30
GLOBAL_FAIL_WINDOW = 3600.0
GLOBAL_LOCKOUT = 900.0
MAX_SSE = 8
HANDSHAKE_TIMEOUT = 15.0
VOICE_INTERVAL = 1.0
WAV_TYPES = ("audio/wav", "audio/x-wav", "audio/wave")
CLIENT_RE = re.compile(r"^[A-Za-z0-9]{8,32}$")
AUDIO_RE = re.compile(r"^/api/audio/([0-9a-f]{16})\.wav$")
MEMORY_NAME_RE = re.compile(r"^(?=.{1,40}$)[a-z0-9]+(-[a-z0-9]+)*$")  # server topic names
PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "media-src 'self' blob:; connect-src 'self'; worker-src 'self'")
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "application/javascript; charset=utf-8", ".json": "application/json",
         ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
         ".wav": "audio/wav"}


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


class _TLSServer(ThreadingHTTPServer):
    """Wraps each accepted socket in TLS inside its worker thread, so a stalled
    handshake never blocks the accept loop."""
    daemon_threads = True
    ssl_context: ssl.SSLContext | None = None

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(HANDSHAKE_TIMEOUT)
            request = self.ssl_context.wrap_socket(
                request, server_side=True, do_handshake_on_connect=False)
            request.settimeout(HANDSHAKE_TIMEOUT)
            request.do_handshake()
            request.settimeout(None)
        except (ssl.SSLError, OSError, ValueError):
            self.shutdown_request(request)
            return
        super().process_request_thread(request, client_address)


class _RedirectHandler(BaseHTTPRequestHandler):
    tls_port = 8443

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _redirect(self):
        host = (self.headers.get("Host") or "").strip()
        if host.startswith("["):
            host = host.split("]")[0] + "]"
        else:
            host = host.split(":")[0]
        if not host or not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
            host = "bmo-pi"
        path = self.path if self.path.startswith("/") else "/"
        self.send_response(301)
        self.send_header("Location", f"https://{host}:{self.tls_port}{path}")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _redirect


def _nets(entries):
    out = []
    for e in entries:
        try:
            out.append(ipaddress.ip_network(e, strict=False))
        except ValueError:
            log.warning("Ignoring bad trusted_proxies entry %r", e)
    return out


def _in_nets(ip: str, nets) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


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
        self._global_fails: list[float] = []
        self._global_locked = 0.0
        self._voice_last: dict[str, float] = {}
        self._sse_count = 0
        self._proxies = _nets(cfg.web.trusted_proxies)
        self._httpd: ThreadingHTTPServer | None = None
        self._redirect: ThreadingHTTPServer | None = None
        self._threads: list[threading.Thread] = []
        self.tls_active = False
        self.url: str | None = None
        self.urls: list[str] = []
        self.https_port: int | None = None
        self.http_port: int | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.port = cfg.web.port

    # -- auth -------------------------------------------------------------
    def check_pin(self, ip: str, pin) -> int:
        """Return 200 on success, 401 on wrong PIN, 429 when locked out."""
        now = time.monotonic()
        with self._lock:
            if self._locked.get(ip, 0) > now or self._global_locked > now:
                return 429
            ok = isinstance(pin, str) and hmac.compare_digest(
                pin.encode(), self._pin.encode())
            if ok:
                self._failures.pop(ip, None)
                return 200
            gf = [t for t in self._global_fails if now - t < GLOBAL_FAIL_WINDOW]
            gf.append(now)
            self._global_fails = gf
            if len(gf) >= GLOBAL_FAIL_LIMIT:
                self._global_locked = now + GLOBAL_LOCKOUT
                self._global_fails = []
                log.warning("Global login lockout: %d failed logins within an hour",
                            len(gf))
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

        web = self.cfg.web
        self._stopping.clear()
        ctx = None
        if web.tls != "off":
            try:
                ctx = tls_mod.build_context(web)
            except Exception as e:  # noqa: BLE001 - BMO must start without TLS
                log.warning("TLS setup failed (%s); serving plain HTTP", e)
        if ctx is not None:
            try:
                httpd = _TLSServer((web.host, web.tls_port), Handler)
                httpd.ssl_context = ctx
                self._httpd = httpd
            except OSError as e:
                log.warning("TLS listener failed (%s); serving plain HTTP", e)
        self.tls_active = self._httpd is not None
        if self._httpd is None:
            self._httpd = ThreadingHTTPServer((web.host, web.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.https_port = self.port if self.tls_active else None
        self.http_port = None if self.tls_active else self.port
        if self.tls_active and web.http_redirect:
            try:
                class Redirect(_RedirectHandler):
                    tls_port = self.port

                self._redirect = ThreadingHTTPServer((web.host, web.port), Redirect)
                self._redirect.daemon_threads = True
                self.http_port = self._redirect.server_address[1]
            except OSError as e:
                log.warning("HTTP redirect listener failed: %s", e)
        scheme = "https" if self.tls_active else "http"
        self.url = f"{scheme}://{web.host}:{self.port}"
        self.urls = [self.url]
        if self._redirect:
            self.urls.append(f"http://{web.host}:{self.http_port}")
        for name, srv in (("web-server", self._httpd), ("web-redirect", self._redirect)):
            if srv:
                t = threading.Thread(target=srv.serve_forever, name=name, daemon=True)
                t.start()
                self._threads.append(t)
        log.info("Web interface on %s", ", ".join(self.urls))

    def stop(self) -> None:
        self._stopping.set()
        for attr in ("_httpd", "_redirect"):
            srv = getattr(self, attr)
            setattr(self, attr, None)
            if srv:
                srv.shutdown()
                srv.server_close()
        for t in self._threads:
            t.join(timeout=5)
        self._threads = []


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
    def _client_ip(self) -> str:
        peer = self.client_address[0]
        if not self.web._proxies or not _in_nets(peer, self.web._proxies):
            return peer
        cf = (self.headers.get("CF-Connecting-IP") or "").strip()
        if cf:
            return cf
        hops = [h.strip() for h in (self.headers.get("X-Forwarded-For") or "").split(",")]
        for h in reversed(hops):
            if h and not _in_nets(h, self.web._proxies):
                return h
        return peer

    def _via_proxy(self) -> bool:
        return bool(self.web._proxies) and _in_nets(self.client_address[0], self.web._proxies)

    def _secure(self) -> bool:
        if isinstance(self.request, ssl.SSLSocket):
            return True
        return (self._via_proxy()
                and (self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = self.headers.get("Host") or ""
        scheme = "https" if self._secure() else "http"
        return origin == f"{scheme}://{host}" or origin in self.web.cfg.web.public_origins

    def _cookie_attrs(self) -> str:
        return "HttpOnly; SameSite=Strict; Path=/" + ("; Secure" if self._secure() else "")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None,
              api: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Permissions-Policy", "microphone=(self), camera=()")
        self.send_header("Referrer-Policy", "same-origin")
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
            state = {"state": c.state.value,
                     "settings": settings_mod.current(self.web.cfg),
                     "text_only": self.web.cfg.ui.text_only}
            info = getattr(c, "session_info", None)
            if callable(info):
                state["session"] = info()
            return self._json(200, state)
        if path == "/api/events":
            return self._events()
        if path.startswith("/api/audio/"):
            return self._audio(path)
        if path == "/api/settings":
            return self._json(200, settings_mod.current(self.web.cfg))
        if path == "/api/status":
            st = dict(c.server_status())
            st["bmo_state"] = c.state.value
            st["live"] = next((e for e in self._snapshot() if e.get("type") == "live"), None)
            return self._json(200, st)
        if path == "/api/memories":
            return self._memories()
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if not self._origin_ok():
            self.close_connection = True
            return self._json(403, {"error": "origin not allowed"})
        if path == "/api/login":
            return self._login()
        if not path.startswith("/api/"):
            self._drain()
            return self._json(404, {"error": "not found"})
        if not self._authed():
            return
        if path == "/api/voice":
            return self._voice()
        if path == "/api/logout":
            self._drain()
            self.web.end_session(self._token())
            return self._json(200, {"ok": True}, {
                "Set-Cookie": f"{COOKIE}=; {self._cookie_attrs()}; Max-Age=0"})
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
        if path == "/api/session/new":
            fn = getattr(self.web.controller, "new_session", None)
            if not callable(fn):
                return self._json(501, {"error": "sessions not supported"})
            return self._json(200, fn())
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
        code = self.web.check_pin(self._client_ip(), data.get("pin"))
        if code == 429:
            return self._json(429, {"error": "too many attempts, try again later"},
                              {"Retry-After": str(int(LOCKOUT))})
        if code != 200:
            return self._json(401, {"error": "wrong PIN"})
        token = self.web.new_session()
        self._json(200, {"ok": True}, {
            "Set-Cookie": f"{COOKIE}={token}; {self._cookie_attrs()}; "
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
        pi = data.get("play_on_pi")
        if pi is not None and not isinstance(pi, bool):
            return self._json(400, {"error": "play_on_pi must be true, false or null"})
        client = data.get("client_id")
        if client is not None and not (isinstance(client, str) and CLIENT_RE.match(client)):
            return self._json(400, {"error": "bad client_id"})
        turn = self.web.controller.submit_text(text.strip(), speak=speak, play_on_pi=pi,
                                               source="web", client_id=client)
        self._json(202, {"ok": True, "turn": turn})

    def _voice(self) -> None:
        w = self.web
        q = parse_qs(urlsplit(self.path).query)

        def flag(name):
            v = q.get(name, [None])[0]
            return None if v is None else (True if v == "1" else False if v == "0" else "bad")

        speak, pi = flag("speak"), flag("pi")
        client = q.get("client", [None])[0]
        if "bad" in (speak, pi) or (client is not None and not CLIENT_RE.match(client)):
            self.close_connection = True
            return self._json(400, {"error": "bad query"})
        token = self._token()
        now = time.monotonic()
        with w._lock:
            last = w._voice_last.get(token, -VOICE_INTERVAL)
            limited = now - last < VOICE_INTERVAL
            if not limited:
                w._voice_last = {k: v for k, v in w._voice_last.items()
                                 if now - v < 60}
                w._voice_last[token] = now
        if limited:
            self.close_connection = True
            return self._json(429, {"error": "too many uploads"}, {"Retry-After": "1"})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in WAV_TYPES:
            self.close_connection = True
            return self._json(415, {"error": "Content-Type must be audio/wav"})
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            length = -1
        if length < 0 or length > w.cfg.web.max_voice_bytes:
            self.close_connection = True
            return self._json(413, {"error": "upload too large or length missing"})
        chunks, left = [], length
        while left:
            buf = self.rfile.read(min(65536, left))
            if not buf:
                self.close_connection = True
                return self._json(400, {"error": "truncated body"})
            chunks.append(buf)
            left -= len(buf)
        data = b"".join(chunks)
        try:
            with wave.open(io.BytesIO(data)) as wf:
                ok = (wf.getcomptype() == "NONE" and wf.getsampwidth() == 2
                      and wf.getnchannels() == 1 and 8000 <= wf.getframerate() <= 48000)
                dur = wf.getnframes() / wf.getframerate() if ok else 0
        except (wave.Error, EOFError, ZeroDivisionError):
            ok, dur = False, 0
        if not ok or not 0.3 <= dur <= 30:
            return self._json(400, {"error": "need 16-bit mono PCM WAV, 8-48 kHz, 0.3-30 s"})
        up = w.cfg.runtime_path / "uploads"
        up.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = up / f"{uuid.uuid4().hex}.wav"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        turn = w.controller.submit_audio(path, speak=speak, play_on_pi=pi,
                                         source="web", client_id=client)
        if turn is None:
            path.unlink(missing_ok=True)
            return self._json(503, {"error": "not accepting audio right now"})
        self._json(202, {"turn": turn})

    def _audio(self, path: str) -> None:
        m = AUDIO_RE.match(path)
        p = self.web.controller.reply_path(m.group(1)) if m else None
        try:
            body = Path(p).read_bytes() if p else None
        except OSError:
            body = None
        if body is None:
            return self._json(404, {"error": "not found"})
        self._send(200, body, "audio/wav", api=True)

    def _snapshot(self) -> list[dict]:
        fn = getattr(self.web.controller, "snapshot", None)
        return list(fn()) if callable(fn) else []

    def _memories(self) -> None:
        c = self.web.controller
        if not all(callable(getattr(c, n, None)) for n in ("list_memories", "conversation")):
            return self._json(501, {"error": "memory not supported"})
        body = {"long_term": c.list_memories(), "conversation": c.conversation()}
        info = getattr(c, "session_info", None)
        if callable(info):
            body["session"] = info()
        self._json(200, body)

    def _memory_call(self, name: str, *args) -> None:
        fn = getattr(self.web.controller, name, None)
        if not callable(fn):
            return self._json(501, {"error": "memory not supported"})
        self._json(200, fn(*args))

    def _memory_delete(self, data: dict) -> None:
        name, mid = data.get("name"), data.get("id")   # topic name (v2 server) or id (v1)
        if isinstance(name, str) and MEMORY_NAME_RE.fullmatch(name):
            return self._memory_call("delete_memory", name)
        if name is None and isinstance(mid, int) and not isinstance(mid, bool) and mid > 0:
            return self._memory_call("delete_memory", mid)
        self._json(400, {"error": "give a memory topic name or id"})

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
        w = self.web
        with w._lock:
            full = w._sse_count >= MAX_SSE
            if not full:
                w._sse_count += 1
        if full:
            return self._json(503, {"error": "too many event streams"})
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
            snap = self._snapshot() if callable(getattr(c, "snapshot", None)) else [
                {"type": "state", "state": c.state.value, "message": "", "time": time.time()}]
            for ev in snap:
                self._sse(ev)
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
            with w._lock:
                w._sse_count -= 1

    def _sse(self, ev: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
        self.wfile.flush()

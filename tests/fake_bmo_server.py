"""In-process fake BMO server for tests (127.0.0.1, ephemeral port)."""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import wave
from email.parser import BytesParser
from email.policy import default as email_default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 160)
    return buf.getvalue()


def parse_multipart(content_type: str, body: bytes) -> dict:
    """Return {field_name: filename_or_None} for a multipart body."""
    msg = BytesParser(policy=email_default).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
    )
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        out[name] = part.get_filename()
    return out


def multipart_values(content_type: str, body: bytes) -> dict:
    """Return {field_name: text} for the non-file parts of a multipart body."""
    msg = BytesParser(policy=email_default).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
    )
    return {part.get_param("name", header="content-disposition"): part.get_content()
            for part in msg.iter_parts() if part.get_filename() is None}


class FakeBMOServer:
    def __init__(self):
        self.requests: list[dict] = []
        self.reservation_responses: list[int] = [200]
        self.status_ready: list[bool] = [True]
        self.auth_mode_401 = False
        self.interact_delay = 0.0
        self.interact_audio_b64: str | None = base64.b64encode(tiny_wav()).decode()
        self.cancel_status = 200
        self.memory_routes = True  # False = the server predates memory (404)
        self.memories: list[dict] = [{"name": "likes-tea", "content": "Likes tea.",
                                      "type": "preference", "created_at": "2026-01-01",
                                      "updated_at": "2026-01-01"}]
        self.session_ended = True
        self.session_end_status = 200   # 403 = v1 server: route not allowed for BMO
        self.interact_extra: dict = {}
        self.cancelled = threading.Event()
        self.interact_started = threading.Event()
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "FakeBMOServer":
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                ctype = self.headers.get("Content-Type", "")
                rec = {
                    "method": self.command,
                    "path": self.path.split("?")[0],
                    "query": self.path.partition("?")[2],
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "json": None,
                    "fields": None,
                }
                if ctype.startswith("application/json") and body:
                    rec["json"] = json.loads(body)
                elif ctype.startswith("multipart/"):
                    rec["fields"] = parse_multipart(ctype, body)
                    rec["values"] = multipart_values(ctype, body)
                    rec["form_body"] = body
                with outer._lock:
                    outer.requests.append(rec)
                status, payload, raw = outer._route(rec)
                if raw is None:
                    raw = json.dumps(payload).encode()
                    ct = "application/json"
                else:
                    ct = "audio/wav"
                self.send_response(status)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_DELETE = _handle

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    # -- helpers -----------------------------------------------------------
    def _pop(self, seq: list):
        with self._lock:
            return seq.pop(0) if len(seq) > 1 else seq[0]

    def find(self, method: str, path: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == method and r["path"] == path]

    def _route(self, rec):
        m, p = rec["method"], rec["path"]
        if p != "/health" and self.auth_mode_401:
            return 401, {"error": "unauthorized"}, None
        if p == "/health":
            return 200, {"status": "ok"}, None
        if p == "/v1/status":
            return 200, {"bmo_ready": self._pop(self.status_ready)}, None
        if p == "/v1/models":
            return 200, {"data": [{"id": "bmo-qwen3-vl-8b"}, {"id": "other"}]}, None
        if p == "/v1/bmo/reservation":
            if m == "DELETE":
                return 204, {}, None
            return self._pop(self.reservation_responses), {"ok": True}, None
        if p == "/v1/bmo/interact":
            outer = self
            outer.interact_started.set()
            if self.interact_delay:
                outer.cancelled.wait(self.interact_delay)
            body = {"transcript": "hello", "text": "hi there", "model": "m",
                    **self.interact_extra}
            if self.interact_audio_b64 is not None:
                body["audio_wav_base64"] = self.interact_audio_b64
            return 200, body, None
        if p == "/v1/audio/transcriptions":
            return 200, {"text": "transcribed"}, None
        if p == "/v1/audio/speech":
            return 200, None, tiny_wav()
        if p == "/v1/chat/completions":
            return 200, {"choices": [{"message": {"content": "chat reply"}}]}, None
        if p == "/v1/bmo/memories" and self.memory_routes:
            if m == "GET":
                return 200, {"memories": list(self.memories)}, None
            if m == "DELETE":
                n, self.memories = len(self.memories), []
                return 200, {"forgotten": n}, None
        if p == "/v1/bmo/session/end" and m == "POST" and self.memory_routes:
            return self.session_end_status, {"ended": self.session_ended}, None
        mem = re.fullmatch(r"/v1/bmo/memories/([^/]+)", p)
        if mem and m == "DELETE" and self.memory_routes:
            keep = [x for x in self.memories if x["name"] != mem.group(1)]
            if len(keep) == len(self.memories):
                return 404, {"error": "unknown topic"}, None
            self.memories = keep
            return 200, {"forgotten": 1}, None
        mm = re.fullmatch(r"/v1/requests/([^/]+)/cancel", p)
        if mm and m == "POST":
            if 200 <= self.cancel_status < 300:
                self.cancelled.set()
            return self.cancel_status, {"cancelled": True}, None
        return 404, {"error": "not found"}, None

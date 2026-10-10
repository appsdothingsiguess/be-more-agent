"""HTTP client for the BMO home server (mirrors tools/bmo_test.py)."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote
from typing import Any

import requests

from app.config import Config, read_token
from app.server.errors import (
    AuthError,
    BadResponse,
    RequestCancelled,
    ServerBusy,
    ServerUnavailable,
)

log = logging.getLogger(__name__)

EMOTIONS = frozenset({"neutral", "happy", "excited", "sad", "surprised", "confused", "angry",
                      "sleepy", "thinking", "blank", "kiss", "dizzy", "straining",
                      "awestruck", "chewing"})


def clean_emotion(value) -> str:
    """The server's face choice, or neutral if missing/odd (older servers send none)."""
    return value if isinstance(value, str) and value in EMOTIONS else "neutral"


@dataclass(frozen=True)
class Action:
    """Something BMO chose to do with its reply: an expression animation or a sound."""
    type: str                       # "expression" or "sound"
    name: str
    audio_wav: bytes | None = None  # sounds only


def clean_actions(value) -> tuple:
    """The server's actions, in order. Odd entries (unknown type, no name, bad audio) are
    dropped one by one so a single bad action never fails the turn."""
    out = []
    for a in value if isinstance(value, list) else ():
        if not isinstance(a, dict) or not isinstance(a.get("name"), str) or not a["name"]:
            continue
        if a.get("type") == "expression":
            out.append(Action("expression", a["name"]))
        elif a.get("type") == "sound":
            try:
                wav = base64.b64decode(a.get("audio_wav_base64") or "", validate=True)
            except (binascii.Error, ValueError, TypeError):
                wav = b""
            if wav:
                out.append(Action("sound", a["name"], wav))
            else:
                log.warning("Dropping sound action %r without valid audio", a["name"])
    return tuple(out)


def clean_statuses(value) -> tuple:
    """Progress messages ("Searching memories..."): plain strings, or dicts with text/message."""
    out = []
    for s in value if isinstance(value, list) else ():
        if isinstance(s, dict):
            s = s.get("text") or s.get("message")
        if isinstance(s, str) and s.strip():
            out.append(s.strip())
    return tuple(out)


@dataclass(frozen=True)
class InteractResult:
    request_id: str
    transcript: str | None
    text: str
    model: str | None
    audio_wav: bytes | None
    raw: dict
    emotion: str = "neutral"   # one of EMOTIONS; anything else becomes neutral
    actions: tuple = ()        # Action, in the order BMO did them
    statuses: tuple = ()       # str


class BMOClient:
    def __init__(
        self,
        cfg: Config,
        token: str | None = None,
        session_id: str | None = None,
        session: requests.Session | None = None,
    ):
        self.cfg = cfg
        self._token = token
        self.session_id = session_id or str(uuid.uuid4())
        self._session = session or requests.Session()
        self._cancel_session = requests.Session()
        self._base = str(cfg.server_url).rstrip("/")

    def __repr__(self) -> str:
        return f"BMOClient(url={self._base}, session={self.session_id})"

    @staticmethod
    def new_request_id() -> str:
        return str(uuid.uuid4())

    # -- internals ---------------------------------------------------------

    def _auth_headers(self, request_id: str | None = None) -> dict[str, str]:
        if self._token is None:
            self._token = read_token(self.cfg)
        h = {
            "Authorization": f"Bearer {self._token}",
            "X-Session-ID": self.session_id,
        }
        if request_id:
            h["X-Request-ID"] = request_id
        return h

    @staticmethod
    def _raise_for(r: requests.Response) -> None:
        code = r.status_code
        if 200 <= code < 300:
            return
        body = (r.text or "")[:200]
        unavailable = BMOClient._unavailable(r)
        if unavailable is not None:
            ucode, message = unavailable
            if "cancel" in ucode:
                raise RequestCancelled(f"request cancelled ({code})")
            raise ServerBusy(ucode, message, status=code)
        if code in (401, 403):
            raise AuthError(f"authentication failed ({code})")
        if code == 499 or (code == 409 and "cancel" in body.lower()):
            raise RequestCancelled(f"request cancelled ({code})")
        if code >= 500:
            raise ServerUnavailable(f"server error {code}")
        raise BadResponse(f"unexpected status {code}", status=code, detail=body)

    @staticmethod
    def _unavailable(r: requests.Response) -> tuple[str, str] | None:
        """(code, display_message) for the scheduler's machine-readable refusals."""
        if "json" not in r.headers.get("content-type", ""):
            return None
        try:
            body = r.json()
        except ValueError:
            return None
        if isinstance(body, dict) and body.get("status") == "unavailable" and body.get("code"):
            return str(body["code"]), str(body.get("display_message") or "")
        return None

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        request_id: str | None = None,
        timeout: tuple[float, float] | None = None,
        ok: tuple[int, ...] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        headers = {**(self._auth_headers(request_id) if auth else {}),
                   **kwargs.pop("headers", {})}
        if timeout is None:
            timeout = (self.cfg.connect_timeout, self.cfg.request_timeout)
        t0 = time.monotonic()
        try:
            r = self._session.request(
                method, self._base + path, headers=headers, timeout=timeout, **kwargs
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            log.warning("%s %s rid=%s failed: %s (%.2fs)", method, path, request_id,
                        type(e).__name__, time.monotonic() - t0)
            raise ServerUnavailable(f"{type(e).__name__} contacting server") from e
        except requests.RequestException as e:
            raise BadResponse(f"request error: {type(e).__name__}") from e
        log.info("%s %s rid=%s status=%d elapsed=%.2fs", method, path, request_id,
                 r.status_code, time.monotonic() - t0)
        if ok is not None and r.status_code in ok:
            return r
        self._raise_for(r)
        return r

    @staticmethod
    def _json(r: requests.Response) -> Any:
        try:
            return r.json()
        except ValueError as e:
            raise BadResponse("invalid JSON in response", status=r.status_code,
                              detail=r.text) from e

    def _short(self, method: str, path: str, **kw: Any) -> requests.Response:
        return self._request(
            method, path, timeout=(self.cfg.connect_timeout, 30), **kw
        )

    # -- simple endpoints --------------------------------------------------

    def health(self) -> dict:
        r = self._request("GET", "/health", auth=False,
                          timeout=(self.cfg.connect_timeout, 30))
        return self._json(r)

    def status(self) -> dict:
        return self._json(self._short("GET", "/v1/status"))

    def models(self) -> list[str]:
        data = self._json(self._short("GET", "/v1/models"))
        try:
            return [m["id"] for m in data["data"]]
        except (KeyError, TypeError) as e:
            raise BadResponse("malformed models response") from e

    def reserve(self) -> int:
        r = self._short("POST", "/v1/bmo/reservation", ok=(200, 202))
        return r.status_code

    def release(self) -> None:
        self._short("DELETE", "/v1/bmo/reservation", ok=(200, 204, 404))

    # -- inference ---------------------------------------------------------

    def interact(
        self,
        *,
        text: str | None = None,
        audio_path: str | None = None,
        image_path: str | None = None,
        speak: bool = True,
        request_id: str | None = None,
        history: list[dict] | None = None,
        memory: bool | None = None,
    ) -> InteractResult:
        if not text and not audio_path:
            raise ValueError("interact requires text or audio_path")
        rid = request_id or self.new_request_id()
        # Always multipart, even for text-only requests.
        files: dict[str, Any] = {"speak": (None, "true" if speak else "false")}
        if text:
            files["text"] = (None, text)
        if history:
            files["history"] = (None, json.dumps(history))
        if memory is not None:
            files["memory"] = (None, "on" if memory else "off")
        handles = []
        try:
            if audio_path:
                f = open(audio_path, "rb")
                handles.append(f)
                files["audio"] = (os.path.basename(audio_path), f, "audio/wav")
            if image_path:
                f = open(image_path, "rb")
                handles.append(f)
                files["image"] = (os.path.basename(image_path), f, "image/jpeg")
            r = self._request("POST", "/v1/bmo/interact", request_id=rid,
                              files=files)
        finally:
            for f in handles:
                f.close()
        body = self._json(r)
        if not isinstance(body, dict):
            raise BadResponse("malformed interact response", status=r.status_code)
        raw = dict(body)
        b64 = raw.pop("audio_wav_base64", None)
        audio = None
        if b64:
            try:
                audio = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError, TypeError) as e:
                raise BadResponse("invalid base64 audio", status=r.status_code) from e
        actions = clean_actions(raw.get("actions"))
        if isinstance(raw.get("actions"), list):   # keep raw small: no sound blobs
            raw["actions"] = [{k: v for k, v in a.items() if k != "audio_wav_base64"}
                              if isinstance(a, dict) else a for a in raw["actions"]]
        return InteractResult(
            request_id=rid,
            transcript=raw.get("transcript"),
            text=raw.get("text") or "",
            model=raw.get("model"),
            audio_wav=audio,
            raw=raw,
            emotion=clean_emotion(raw.get("emotion")),
            actions=actions,
            statuses=clean_statuses(raw.get("statuses")),
        )

    # -- long-term memory (404 = server has no memory routes) --------------

    def list_memories(self, limit: int = 50, offset: int = 0) -> list[dict] | None:
        r = self._short("GET", "/v1/bmo/memories", ok=(404,),
                        params={"limit": limit, "offset": offset})
        if r.status_code == 404:
            return None
        body = self._json(r)
        try:
            return list(body["memories"])
        except (KeyError, TypeError) as e:
            raise BadResponse("malformed memories response", status=r.status_code) from e

    def forget_memories(self) -> int | None:
        r = self._short("DELETE", "/v1/bmo/memories", ok=(404,))
        if r.status_code == 404:
            return None
        body = self._json(r)
        try:
            return int(body["forgotten"])
        except (KeyError, TypeError, ValueError) as e:
            raise BadResponse("malformed forget response", status=r.status_code) from e

    def delete_memory(self, key: str | int) -> bool | None:
        """Delete one memory: a topic name (v2 server) or a numeric id (v1).
        None = no memory routes or no such memory (both 404)."""
        r = self._short("DELETE", f"/v1/bmo/memories/{quote(str(key), safe='')}", ok=(404,))
        return None if r.status_code == 404 else True

    def end_session(self, session_id: str) -> bool | None:
        """End a conversation so the server consolidates its logged turns into long-term
        memory. True/False = ended/had no logged turns; None = memory is off (404) or the
        server predates sessions (v1 saves memories every turn instead)."""
        # v1: 403 "not authorized for this route", or 405 when /memories/{id} catches it.
        r = self._short("POST", "/v1/bmo/session/end", ok=(403, 404, 405),
                        headers={"X-Session-ID": session_id})
        if r.status_code in (403, 404, 405):
            return None
        body = self._json(r)
        return bool(body.get("ended")) if isinstance(body, dict) else False

    def transcribe(self, audio_path: str, request_id: str | None = None) -> str:
        rid = request_id or self.new_request_id()
        with open(audio_path, "rb") as f:
            r = self._request(
                "POST", "/v1/audio/transcriptions", request_id=rid,
                files={"file": (os.path.basename(audio_path), f, "audio/wav")},
            )
        body = self._json(r)
        try:
            return body["text"]
        except (KeyError, TypeError) as e:
            raise BadResponse("malformed transcription response") from e

    def speech(self, text: str, request_id: str | None = None) -> bytes:
        rid = request_id or self.new_request_id()
        r = self._request("POST", "/v1/audio/speech", request_id=rid,
                          json={"input": text})
        if not r.content.startswith(b"RIFF"):
            raise BadResponse("speech response is not WAV", status=r.status_code)
        return r.content

    def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int | None = None,
        request_id: str | None = None,
    ) -> str:
        rid = request_id or self.new_request_id()
        payload: dict[str, Any] = {"model": self.cfg.model, "messages": messages}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        r = self._request("POST", "/v1/chat/completions", request_id=rid,
                          json=payload)
        body = self._json(r)
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise BadResponse("malformed chat response") from e

    def cancel(self, request_id: str) -> bool:
        """Best-effort cancel; uses its own Session so it is safe from another thread."""
        t0 = time.monotonic()
        try:
            r = self._cancel_session.post(
                f"{self._base}/v1/requests/{request_id}/cancel",
                headers=self._auth_headers(),
                timeout=(self.cfg.connect_timeout, 10),
            )
        except Exception as e:
            log.warning("cancel rid=%s failed: %s", request_id, type(e).__name__)
            return False
        log.info("POST cancel rid=%s status=%d elapsed=%.2fs", request_id,
                 r.status_code, time.monotonic() - t0)
        if 200 <= r.status_code < 300:
            return True
        log.warning("cancel rid=%s returned %d", request_id, r.status_code)
        return False

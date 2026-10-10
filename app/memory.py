"""Short-term conversation memory: the last few exchanges, persisted to runtime/memory.json.

Sent to the server as ``history`` on every turn. Text only, always whole
user/assistant pairs starting with a user message.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

_PUNCT = re.compile(r"[^\w\s]|_")
_FORGET_PHRASES = ("forget everything", "reset memory")


def is_forget_command(text: str) -> bool:
    """True when the whole utterance is a request to wipe memory (same rule as the server)."""
    words = _PUNCT.sub(" ", text.lower()).split()
    if words and words[0] == "bmo":
        words = words[1:]
    if words and words[-1] == "please":
        words = words[:-1]
    return " ".join(words) in _FORGET_PHRASES


_NEW_SESSION_PHRASES = ("new session", "new conversation", "start over", "start a new conversation",
                        "start a new session", "new chat", "start a new chat")


def is_new_session_command(text: str) -> bool:
    """True when the whole utterance asks to start a fresh conversation."""
    words = _PUNCT.sub(" ", text.lower()).split()
    if words[:2] == ["hey", "bmo"]:
        words = words[2:]
    elif words and words[0] == "bmo":
        words = words[1:]
    if words and words[-1] == "please":
        words = words[:-1]
    return " ".join(words) in _NEW_SESSION_PHRASES


_GOODBYE_PHRASES = frozenset((
    "bye", "bye bye", "goodbye", "good bye", "bye for now", "see you", "see ya", "see you later",
    "see you soon", "see you tomorrow", "talk to you later", "talk later", "later", "catch you later",
    "good night", "goodnight", "night night", "that s all", "that is all", "that s all for now",
    "that will be all", "that ll be all", "i m done", "we re done", "all done", "stop listening",
    "go to sleep", "nothing else", "that s it", "that s it for now"))
_GOODBYE_FILLER = frozenset(("hey", "bmo", "beemo", "okay", "ok", "alright", "all", "right", "well",
                             "thanks", "thank", "you", "so", "for", "now", "then", "please", "buddy"))


def is_goodbye(text: str) -> bool:
    """True when the whole utterance is a farewell ("Thanks BMO, goodbye!", "that's all for now")."""
    words = _PUNCT.sub(" ", text.lower()).split()
    if " ".join(words) in _GOODBYE_PHRASES:
        return True
    # Peel filler words off both ends ("okay thanks bmo ... bye", "bye bmo"), then match.
    for _ in range(2):
        while words and words[0] in _GOODBYE_FILLER and " ".join(words) not in _GOODBYE_PHRASES:
            words = words[1:]
        while words and words[-1] in _GOODBYE_FILLER and " ".join(words) not in _GOODBYE_PHRASES:
            words = words[:-1]
    return bool(words) and " ".join(words) in _GOODBYE_PHRASES


def write_json_atomic(path: Path, data, prefix: str = ".tmp-") -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=prefix)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.write("\n")
        os.replace(tmp, path)
        return True
    except OSError as e:
        log.warning("Could not save %s: %s", path, e)
        return False


class ConversationSession:
    """Full text of the current conversation, persisted to runtime/session.json.

    Separate from ConversationMemory (the short window sent with each turn): this is
    what gets handed to the server for long-term consolidation when the session ends.
    """

    MAX_MESSAGES = 80
    MAX_CHARS = 40000

    def __init__(self, path: Path, max_messages: int = MAX_MESSAGES, max_chars: int = MAX_CHARS,
                 clock=time.time):
        self.path = Path(path)
        self.max_messages = max_messages
        self.max_chars = max_chars
        self._clock = clock
        self._lock = threading.Lock()
        self._new()
        self._load()

    def _new(self) -> None:
        now = self._clock()
        self.id = uuid.uuid4().hex
        self.started_at = now
        self.last_activity = now
        self._messages: list[dict] = []

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("Ignoring unreadable session file %s: %s", self.path, e)
            return
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            log.warning("Ignoring invalid session file %s", self.path)
            return
        self.id = data["id"]
        for key in ("started_at", "last_activity"):
            v = data.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                setattr(self, key, float(v))
        for m in data.get("messages") or []:
            if (isinstance(m, dict) and m.get("role") in ("user", "assistant")
                    and isinstance(m.get("content"), str) and m["content"].strip()):
                self._messages.append({"role": m["role"], "content": m["content"]})
        self._trim()

    def _trim(self) -> None:
        while self._messages and (
            len(self._messages) > self.max_messages
            or sum(len(m["content"]) for m in self._messages) > self.max_chars
        ):
            del self._messages[:2]

    def _save(self) -> None:
        write_json_atomic(self.path, self.to_dict(locked=True), ".session-")

    def to_dict(self, locked: bool = False) -> dict:
        def build():
            return {"id": self.id, "started_at": self.started_at,
                    "last_activity": self.last_activity,
                    "messages": [dict(m) for m in self._messages]}
        if locked:
            return build()
        with self._lock:
            return build()

    def add_exchange(self, user_text: str, assistant_text: str) -> None:
        user_text, assistant_text = user_text.strip(), assistant_text.strip()
        if not user_text or not assistant_text:
            return
        with self._lock:
            self._messages.append({"role": "user", "content": user_text})
            self._messages.append({"role": "assistant", "content": assistant_text})
            self._trim()
            self.last_activity = self._clock()
            self._save()

    def touch(self) -> None:
        """Record activity in memory (persisted with the next exchange)."""
        self.last_activity = self._clock()

    def messages(self) -> list[dict]:
        with self._lock:
            return [dict(m) for m in self._messages]

    @property
    def is_empty(self) -> bool:
        with self._lock:
            return not self._messages

    def idle_seconds(self, now: float | None = None) -> float:
        return (self._clock() if now is None else now) - self.last_activity

    def reset(self) -> str:
        """Start a new empty session; returns its id."""
        with self._lock:
            self._new()
            self._save()
            return self.id

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)


class ConversationMemory:
    def __init__(self, path: Path, max_messages: int = 10, max_chars: int = 6000):
        self.path = Path(path)
        self.max_messages = max_messages
        self.max_chars = max_chars
        self._lock = threading.Lock()
        self._messages: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("Ignoring unreadable memory file %s: %s", self.path, e)
            return
        if not isinstance(data, list):
            log.warning("Ignoring invalid memory file %s", self.path)
            return
        for m in data:
            if (isinstance(m, dict) and m.get("role") in ("user", "assistant")
                    and isinstance(m.get("content"), str) and m["content"].strip()):
                self._messages.append({"role": m["role"], "content": m["content"]})
        while self._messages and self._messages[0]["role"] != "user":
            del self._messages[0]
        self._trim()

    def _trim(self) -> None:
        while self._messages and (
            len(self._messages) > self.max_messages
            or sum(len(m["content"]) for m in self._messages) > self.max_chars
        ):
            del self._messages[:2]
        # A hand-edited file could leave a dangling user message.
        if self._messages and self._messages[-1]["role"] == "user":
            self._messages.pop()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".memory-")
            with os.fdopen(fd, "w") as f:
                json.dump(self._messages, f)
                f.write("\n")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Could not save memory to %s: %s", self.path, e)

    def add_exchange(self, user_text: str, assistant_text: str) -> None:
        user_text, assistant_text = user_text.strip(), assistant_text.strip()
        if not user_text or not assistant_text:
            return
        with self._lock:
            self._messages.append({"role": "user", "content": user_text})
            self._messages.append({"role": "assistant", "content": assistant_text})
            self._trim()
            self._save()

    def messages(self) -> list[dict]:
        with self._lock:
            return [dict(m) for m in self._messages]

    def clear(self) -> None:
        with self._lock:
            self._messages = []
            self._save()

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)

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

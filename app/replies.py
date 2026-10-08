"""Reply audio store: the last few spoken replies, served to the web page by turn id.

Turn ids are random 16-character hex strings (unguessable, filename-safe).
"""

from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path

log = logging.getLogger(__name__)

TURN_ID_RE = re.compile(r"^[0-9a-f]{16}$")


class ReplyStore:
    def __init__(self, directory: str | Path, keep: int = 8):
        self.dir = Path(directory)
        self.keep = keep

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(8)

    def save(self, turn_id: str, data: bytes) -> Path:
        if not TURN_ID_RE.match(turn_id):
            raise ValueError("invalid turn id")
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.dir / f"{turn_id}.wav"
        target.write_bytes(data)
        self.prune()
        return target

    def path(self, turn_id: str) -> Path | None:
        if not isinstance(turn_id, str) or not TURN_ID_RE.match(turn_id):
            return None
        target = self.dir / f"{turn_id}.wav"
        return target if target.is_file() else None

    def prune(self) -> None:
        """Keep only the newest `keep` replies."""
        try:
            files = sorted(self.dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for old in files[self.keep:]:
            try:
                old.unlink()
            except OSError as e:
                log.warning("Could not delete old reply %s: %s", old, e)

    @staticmethod
    def purge_legacy(runtime_dir: str | Path) -> None:
        """Delete leftovers of the old layout: runtime/reply-*.wav and runtime/uploads/*."""
        runtime = Path(runtime_dir)
        doomed = list(runtime.glob("reply-*.wav"))
        uploads = runtime / "uploads"
        if uploads.is_dir():
            doomed += [p for p in uploads.iterdir() if p.is_file() or p.is_symlink()]
        for p in doomed:
            try:
                p.unlink()
            except OSError as e:
                log.warning("Could not delete %s: %s", p, e)

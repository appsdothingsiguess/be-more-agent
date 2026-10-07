"""Keyboard input: key-name normalisation, keymap lookup and a stdlib evdev reader."""
from __future__ import annotations

import logging
import os
import select
import struct
import threading
from enum import Enum
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class Action(str, Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    A = "a"
    B = "b"
    START = "start"
    QUIT = "quit"
    INTERRUPT = "interrupt"


def normalize_key(name: str) -> str:
    """'KEY_ENTER' -> 'enter', 'Return' -> 'return', 'Up' -> 'up'."""
    name = name.strip().lower()
    if name.startswith("key_"):
        name = name[4:]
    return name


class KeyMapper:
    def __init__(self, keymap: dict[str, str]):
        self._map: dict[str, Action] = {}
        for key, action in keymap.items():
            try:
                self._map[normalize_key(key)] = Action(str(action).lower())
            except ValueError:
                log.warning("Ignoring unknown action %r for key %r", action, key)

    def action_for(self, keyname: str) -> Action | None:
        return self._map.get(normalize_key(keyname))


# --- evdev (struct input_event: timeval sec, usec, type, code, value) ---

EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EV_KEY = 1
KEY_DOWN = 1

KEYCODES: dict[int, str] = {
    1: "esc", 28: "enter", 57: "space", 96: "kpenter",
    30: "a", 48: "b", 45: "x", 44: "z",
    103: "up", 108: "down", 105: "left", 106: "right",
    14: "backspace", 15: "tab",
}
KEYCODES.update({59 + i: f"f{i + 1}" for i in range(10)})  # F1-F10
KEYCODES.update({87: "f11", 88: "f12"})


def decode_events(data: bytes) -> list[tuple[int, int, int]]:
    """Decode packed input_events into (type, code, value); trailing partial data is ignored."""
    out = []
    for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
        _s, _us, etype, code, value = struct.unpack_from(EVENT_FORMAT, data, off)
        out.append((etype, code, value))
    return out


def discover_keyboards(by_id_dir: str | Path = "/dev/input/by-id") -> list[Path]:
    d = Path(by_id_dir)
    try:
        return sorted(d.glob("*-event-kbd"))
    except OSError:
        return []


class EvdevReader:
    def __init__(self, paths, mapper: KeyMapper, callback: Callable[[Action], None]):
        self.paths = [Path(p) for p in paths]
        self.mapper = mapper
        self.callback = callback
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for path in self.paths:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as e:
                log.warning("Cannot open input device %s: %s", path, e)
                continue
            t = threading.Thread(target=self._read_loop, args=(fd, path), daemon=True)
            t.start()
            self._threads.append(t)

    def _read_loop(self, fd: int, path: Path) -> None:
        buf = b""
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([fd], [], [], 0.2)
                if not ready:
                    continue
                try:
                    chunk = os.read(fd, EVENT_SIZE * 32)
                except BlockingIOError:
                    continue
                except OSError as e:
                    log.warning("Input device %s failed: %s", path, e)
                    return
                if not chunk:  # writer closed (pipe/FIFO) or device gone
                    if self._stop.wait(0.2):
                        return
                    continue
                buf += chunk
                usable = len(buf) - len(buf) % EVENT_SIZE
                data, buf = buf[:usable], buf[usable:]
                for etype, code, value in decode_events(data):
                    if etype != EV_KEY or value != KEY_DOWN:
                        continue
                    name = KEYCODES.get(code)
                    action = self.mapper.action_for(name) if name else None
                    if action is not None:
                        try:
                            self.callback(action)
                        except Exception:
                            log.exception("Input callback failed")
        finally:
            os.close(fd)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads.clear()

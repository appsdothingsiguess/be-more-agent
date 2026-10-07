import os
import struct
import time

from app.config import InputConfig
from app.hardware.input import (
    EV_KEY, EVENT_FORMAT, KEYCODES, Action, EvdevReader, KeyMapper,
    decode_events, discover_keyboards, normalize_key)


def ev(code, value, etype=EV_KEY):
    return struct.pack(EVENT_FORMAT, 0, 0, etype, code, value)


def test_normalize():
    assert normalize_key("KEY_ENTER") == "enter"
    assert normalize_key("Return") == "return"
    assert normalize_key("Up") == "up"
    assert normalize_key("Escape") == "escape"
    assert normalize_key("space") == "space"


def test_mapper_default_keymap():
    m = KeyMapper(InputConfig().keymap)
    assert m.action_for("Return") == Action.START
    assert m.action_for("KEY_ENTER") == Action.START
    assert m.action_for("Escape") is None  # Esc no longer quits (stuck service)
    assert m.action_for("Up") == Action.UP
    assert m.action_for("KEY_Z") == Action.A
    assert m.action_for("f9") is None


def test_decode_and_ignore_up_repeat():
    data = ev(28, 1) + ev(28, 0) + ev(28, 2) + ev(0, 0, etype=0)
    assert decode_events(data) == [(1, 28, 1), (1, 28, 0), (1, 28, 2), (0, 0, 0)]
    assert decode_events(data[:-3]) == decode_events(data)[:-1]
    assert KEYCODES[28] == "enter" and KEYCODES[59] == "f1"


def test_discover(tmp_path):
    (tmp_path / "x-event-kbd").symlink_to("/dev/null")
    (tmp_path / "y-event-mouse").symlink_to("/dev/null")
    found = discover_keyboards(tmp_path)
    assert [p.name for p in found] == ["x-event-kbd"]
    assert discover_keyboards(tmp_path / "missing") == []


def test_reader_fifo(tmp_path):
    fifo = tmp_path / "kbd"
    os.mkfifo(fifo)
    got = []
    r = EvdevReader([fifo, tmp_path / "nope"], KeyMapper(InputConfig().keymap), got.append)
    r.start()
    wfd = os.open(fifo, os.O_WRONLY)
    # key-down ENTER, up, repeat (ignored), key-down B
    os.write(wfd, ev(28, 1) + ev(28, 0) + ev(28, 2) + ev(48, 1))
    deadline = time.time() + 3
    while len(got) < 2 and time.time() < deadline:
        time.sleep(0.05)
    r.stop()
    os.close(wfd)
    assert got == [Action.START, Action.B]

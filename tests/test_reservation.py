import pytest

from app.config import Config
from app.server import Reservation, ReservationState, ReservationTimeout


class FakeClient:
    def __init__(self, reserves, ready):
        self.reserves = list(reserves)
        self.ready = list(ready)
        self.calls = []

    def reserve(self):
        self.calls.append("reserve")
        return self.reserves.pop(0)

    def status(self):
        self.calls.append("status")
        v = self.ready.pop(0) if len(self.ready) > 1 else self.ready[0]
        return {"bmo_ready": v}

    def release(self):
        self.calls.append("release")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(reserves, ready=(True,)):
    cfg = Config(readiness_timeout=30.0, readiness_poll_seconds=3.0,
                 reservation_renew_after=100.0)
    c = FakeClient(reserves, ready)
    clk = Clock()
    return Reservation(c, cfg, clock=clk, sleep=clk.sleep), c, clk


def test_200_path():
    r, c, _ = make([200])
    r.ensure_ready()
    assert r.state == ReservationState.READY
    assert c.calls == ["reserve"]


def test_202_poll_200():
    r, c, clk = make([202, 200], ready=[False, False, True])
    r.ensure_ready()
    assert r.state == ReservationState.READY
    assert c.calls == ["reserve", "status", "status", "status", "reserve"]
    assert clk.t == 1006.0


def test_timeout():
    r, c, _ = make([202], ready=[False])
    with pytest.raises(ReservationTimeout):
        r.ensure_ready()
    assert r.state == ReservationState.RESTORING


def test_202_twice_keeps_polling():
    r, c, _ = make([202, 202, 200])
    r.ensure_ready()
    assert c.calls.count("reserve") == 3


def test_renew_window_skip():
    r, c, clk = make([200, 200])
    r.ensure_ready()
    clk.t += 50
    r.ensure_ready()
    assert c.calls == ["reserve"]
    clk.t += 60
    r.ensure_ready()
    assert c.calls == ["reserve", "reserve"]


def test_renewed_extends_window():
    r, c, clk = make([200])
    r.ensure_ready()
    clk.t += 90
    r.renewed()
    clk.t += 90
    r.ensure_ready()
    assert c.calls == ["reserve"]


def test_mark_stale_forces_status_and_post():
    r, c, _ = make([200, 200])
    r.ensure_ready()
    r.mark_stale()
    assert r.state == ReservationState.STALE
    r.ensure_ready()
    assert c.calls == ["reserve", "status", "reserve"]
    assert r.state == ReservationState.READY


def test_release_idempotent():
    r, c, _ = make([200])
    r.release()
    assert c.calls == []
    r.ensure_ready()
    r.release()
    r.release()
    assert c.calls.count("release") == 1
    assert r.state == ReservationState.RELEASED

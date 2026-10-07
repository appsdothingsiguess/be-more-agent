"""BMO GPU reservation state machine (logic from tools/bmo_test.py reserve())."""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum

from app.config import Config
from app.server.errors import BadResponse, ReservationTimeout

log = logging.getLogger(__name__)


class ReservationState(str, Enum):
    RELEASED = "released"
    RESTORING = "restoring"
    READY = "ready"
    STALE = "stale"


class Reservation:
    def __init__(self, client, cfg: Config, clock=time.monotonic, sleep=time.sleep):
        self._client = client
        self._cfg = cfg
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._state = ReservationState.RELEASED
        self.last_renewed: float | None = None
        # Called with each /v1/status dict while waiting for bmo_ready (mode,
        # queue counts...), so the UI can say why BMO is waiting.
        self.on_wait = None

    @property
    def state(self) -> ReservationState:
        return self._state

    def ensure_ready(self) -> None:
        with self._lock:
            if (
                self._state == ReservationState.READY
                and self.last_renewed is not None
                and (self._clock() - self.last_renewed) < self._cfg.reservation_renew_after
            ):
                return
            deadline = self._clock() + self._cfg.readiness_timeout
            if self._state == ReservationState.STALE:
                self._state = ReservationState.RESTORING
                self._wait_ready(deadline)
            code = self._client.reserve()
            while code == 202:
                self._state = ReservationState.RESTORING
                self._wait_ready(deadline)
                code = self._client.reserve()
            if code != 200:
                raise BadResponse(f"unexpected reservation status {code}", status=code)
            self._mark_ready()

    def _mark_ready(self) -> None:
        self._state = ReservationState.READY
        self.last_renewed = self._clock()

    def _wait_ready(self, deadline: float) -> None:
        while True:
            status = self._client.status()
            if status.get("bmo_ready"):
                return
            if self.on_wait is not None:
                try:
                    self.on_wait(status)
                except Exception:
                    log.exception("on_wait callback failed")
            if self._clock() >= deadline:
                raise ReservationTimeout("BMO workers not ready before timeout")
            self._sleep(self._cfg.readiness_poll_seconds)
            if self._clock() >= deadline:
                raise ReservationTimeout("BMO workers not ready before timeout")

    def renewed(self) -> None:
        with self._lock:
            self._mark_ready()

    def mark_stale(self) -> None:
        with self._lock:
            self._state = ReservationState.STALE

    def release(self) -> None:
        with self._lock:
            if self._state == ReservationState.RELEASED:
                return
            try:
                self._client.release()
            except Exception as e:
                log.warning("release failed: %s", type(e).__name__)
            self._state = ReservationState.RELEASED

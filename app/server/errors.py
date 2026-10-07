"""Exceptions raised by the BMO server client."""

from __future__ import annotations


class BMOError(Exception):
    pass


class AuthError(BMOError):
    """401/403 from the server."""


class ServerUnavailable(BMOError):
    """Connection error, timeout or 5xx."""


class RequestCancelled(BMOError):
    pass


class BadResponse(BMOError):
    def __init__(self, message: str, status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = (detail or "")[:200]


class ReservationTimeout(BMOError):
    pass


class ServerBusy(BMOError):
    """The scheduler refused work for now: {"status": "unavailable", "code", "display_message"}.

    Codes include large_model_session_active, mode_transition, bmo_restoring,
    gpu_unavailable, bmo_busy_dual, bmo_reserved and transition_failed.
    """

    def __init__(self, code: str, display_message: str = "", status: int | None = None):
        super().__init__(f"server busy: {code}")
        self.code = code
        self.display_message = display_message
        self.status = status

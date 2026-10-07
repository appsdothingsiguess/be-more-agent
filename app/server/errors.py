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

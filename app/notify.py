"""User-facing errors: what to show, what to try next, and a pre-recorded clip.

Clips live in sounds/errors/<code>.wav and are generated once with
tools/make_error_clips.py (server voice), so they still play when the server
is down. In text-only (mute) mode nothing is played; the message is shown on
the screen / console and sent to the web page as an "error" event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ErrorInfo:
    message: str   # what went wrong (shown and spoken)
    hint: str      # short next step (shown and spoken)


ERRORS: dict[str, ErrorInfo] = {
    "server_unreachable": ErrorInfo(
        "I can't reach my brain server.",
        "Check that the computer is on and on the network, then press Start to try again."),
    "auth_failed": ErrorInfo(
        "My brain server doesn't recognise me.",
        "My access key needs to be checked on the Pi."),
    "server_busy_large_model": ErrorInfo(
        "My brain server is busy running a big AI model.",
        "Try again when it finishes."),
    "server_switching": ErrorInfo(
        "My brain server is switching things around.",
        "Give it a minute, then press Start to try again."),
    "bmo_restoring": ErrorInfo(
        "I'm still waking up on my brain server.",
        "Wait a moment, then press Start to try again."),
    "gpu_unavailable": ErrorInfo(
        "My brain server's graphics cards aren't available.",
        "Check the server status, then try again."),
    "gpu_wait_timeout": ErrorInfo(
        "I waited too long for my brain server to get ready.",
        "Press Start to try again, or check the server."),
    "mic_failed": ErrorInfo(
        "My microphone isn't working.",
        "Check that the microphone is plugged in."),
    "nothing_heard": ErrorInfo(
        "I didn't hear anything.",
        "Press Start, talk, then press Start again."),
    "camera_failed": ErrorInfo(
        "My camera isn't working, so I answered without looking.",
        "Check the camera cable."),
    "cancelled": ErrorInfo(
        "Okay, I stopped.",
        "Press Start when you want to talk again."),
    "server_waiting": ErrorInfo(
        "Hold on, my brain server is getting ready.",
        "This can take a minute."),
    "unknown": ErrorInfo(
        "Oops, something went wrong.",
        "Press Start to try again."),
}

# Scheduler refusal codes (server scheduler.py / server.py) -> our catalogue.
SERVER_CODES: dict[str, str] = {
    "large_model_session_active": "server_busy_large_model",
    "bmo_busy_dual": "server_busy_large_model",
    "mode_transition": "server_switching",
    "transition_failed": "server_switching",
    "bmo_reserved": "server_switching",
    "bmo_restoring": "bmo_restoring",
    "gpu_unavailable": "gpu_unavailable",
}

# Codes that only inform (BMO returns to idle instead of the error state).
SOFT = {"nothing_heard", "cancelled", "camera_failed"}
# Progress notices: shown/spoken, but the turn keeps going (no state change).
NOTICES = {"server_waiting"}


def clip_path(sounds_dir: Path, code: str) -> Path:
    return Path(sounds_dir) / "errors" / f"{code}.wav"


def spoken_text(code: str) -> str:
    info = ERRORS[code]
    return f"{info.message} {info.hint}"

"""UI state machine definitions and the Controller/UI protocols."""
from __future__ import annotations

from enum import Enum
from typing import Protocol

from app.hardware.input import Action


class BotState(str, Enum):
    WARMUP = "warmup"
    IDLE = "idle"
    LISTENING = "listening"
    CAPTURING = "capturing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"


_S = BotState
TRANSITIONS: dict[BotState, set[BotState]] = {
    _S.WARMUP: {_S.IDLE, _S.ERROR},
    _S.IDLE: {_S.LISTENING, _S.THINKING, _S.CAPTURING, _S.ERROR},
    _S.LISTENING: {_S.CAPTURING, _S.THINKING, _S.IDLE, _S.ERROR},
    _S.CAPTURING: {_S.THINKING, _S.IDLE, _S.ERROR},
    _S.THINKING: {_S.SPEAKING, _S.IDLE, _S.ERROR},
    _S.SPEAKING: {_S.IDLE, _S.LISTENING, _S.ERROR},
    _S.ERROR: {_S.IDLE, _S.WARMUP},
}


def can_transition(a: BotState, b: BotState) -> bool:
    return a == b or b in TRANSITIONS.get(a, set())


class Controller(Protocol):
    def handle_action(self, action: Action) -> None: ...
    def submit_text(self, text: str, speak: bool | None = None, *, play_on_pi: bool | None = None,
                    source: str = "web", client_id: str | None = None) -> str | None: ...
    def start_listening(self, source: str = "button") -> bool: ...
    def finish_listening(self) -> None: ...
    def cancel_listening(self, quiet: bool = False) -> None: ...
    def shutdown(self) -> None: ...


class UI(Protocol):
    def set_state(self, state: BotState, message: str = "") -> None: ...
    def show_text(self, text: str, who: str = "bmo") -> None: ...
    def run(self, controller: Controller) -> None: ...
    def close(self) -> None: ...

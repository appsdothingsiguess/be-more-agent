"""Terminal front end for machines without a display."""
from __future__ import annotations

import sys
import threading

from app.hardware.input import Action
from app.ui.states import BotState, Controller

HELP = (
    "Headless mode. Enter = start/stop, i = interrupt, a / b = buttons, "
    "t <text> = send text, q = quit."
)


class HeadlessUI:
    def __init__(self, stdin=None, out=None):
        self._stdin = stdin if stdin is not None else sys.stdin
        self._out = out or (lambda s: print(s, flush=True))
        self._lock = threading.Lock()

    def _emit(self, line: str) -> None:
        with self._lock:
            self._out(line)

    def set_state(self, state: BotState, message: str = "") -> None:
        label = BotState(state).name
        self._emit(f"[{label}] {message}".rstrip())

    def show_text(self, text: str, who: str = "bmo") -> None:
        self._emit(f"{'You' if who == 'user' else 'BMO'}: {text}")

    def run(self, controller: Controller) -> None:
        self._emit(HELP)
        try:
            while True:
                line = self._stdin.readline()
                if not line:  # EOF
                    controller.handle_action(Action.QUIT)
                    break
                cmd = line.strip()
                if cmd == "":
                    controller.handle_action(Action.START)
                elif cmd == "i":
                    controller.handle_action(Action.INTERRUPT)
                elif cmd == "a":
                    controller.handle_action(Action.A)
                elif cmd == "b":
                    controller.handle_action(Action.B)
                elif cmd == "q":
                    controller.handle_action(Action.QUIT)
                    break
                elif cmd.startswith("t ") and cmd[2:].strip():
                    controller.submit_text(cmd[2:].strip())
                else:
                    self._emit(HELP)
        except KeyboardInterrupt:
            pass
        finally:
            controller.shutdown()

    def close(self) -> None:
        pass

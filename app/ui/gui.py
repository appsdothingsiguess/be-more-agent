"""Tk face GUI. tkinter / PIL.ImageTk are imported lazily so headless boxes work."""
from __future__ import annotations

import logging
import os
import random
from pathlib import Path

from app.config import UIConfig
from app.hardware.input import Action, KeyMapper
from app.ui.states import BotState, Controller

log = logging.getLogger(__name__)

SPEAK_FRAME_MS = 50
FRAME_MS = 500


def load_face_frames(faces_dir: Path) -> dict[BotState, list[Path]]:
    faces_dir = Path(faces_dir)
    out: dict[BotState, list[Path]] = {}
    for state in BotState:
        d = faces_dir / state.value
        out[state] = sorted(d.glob("*.png"), key=lambda p: p.name) if d.is_dir() else []
    return out


def gui_available() -> bool:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    return True


class TkUI:
    def __init__(self, cfg: UIConfig, faces_dir: Path, keymap: dict[str, str]):
        import tkinter as tk
        from tkinter import ttk

        from PIL import Image, ImageTk

        self._tk, self._Image, self._ImageTk = tk, Image, ImageTk
        self.cfg = cfg
        self.mapper = KeyMapper(keymap)
        self.controller: Controller | None = None
        self.state = BotState.WARMUP
        self.frame_index = 0
        self._closed = False

        self.root = tk.Tk()
        self.root.title("BMO")
        self.root.configure(bg="black")
        if cfg.fullscreen:
            self.root.attributes("-fullscreen", True)
        else:
            self.root.geometry(f"{cfg.width}x{cfg.height}")
        self.w, self.h = cfg.width, cfg.height

        self.background_label = tk.Label(self.root, bg="black")
        self.background_label.place(x=0, y=0, relwidth=1, relheight=1)
        self.background_label.bind("<Button-1>", self.toggle_hud_visibility)

        self.response_text = tk.Text(
            self.root, height=6, width=60, wrap=tk.WORD, state=tk.DISABLED,
            bg="#ffffff", fg="#000000", font=("Arial", 12))
        self.status_var = tk.StringVar(value="Initializing...")
        self.status_label = ttk.Label(
            self.root, textvariable=self.status_var, background="#2e2e2e", foreground="white")

        self.root.bind("<Key>", self._on_key)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

        self.animations = self._load_animations(Path(faces_dir))
        self._update_animation()

    def _load_animations(self, faces_dir: Path) -> dict:
        anims = {}
        for state, paths in load_face_frames(faces_dir).items():
            frames = []
            for p in paths:
                try:
                    img = self._Image.open(p).resize((self.w, self.h))
                    frames.append(self._ImageTk.PhotoImage(img))
                except Exception:
                    log.warning("Failed to load face frame %s", p)
            if not frames:
                blank = self._Image.new("RGB", (self.w, self.h), color="#0000FF")
                frames.append(self._ImageTk.PhotoImage(blank))
            anims[state] = frames
        return anims

    def _update_animation(self) -> None:
        if self._closed:
            return
        frames = self.animations.get(self.state) or self.animations.get(BotState.IDLE, [])
        if not frames:
            self.root.after(FRAME_MS, self._update_animation)
            return
        if self.state == BotState.SPEAKING:
            self.frame_index = random.randint(1, len(frames) - 1) if len(frames) > 1 else 0
        else:
            self.frame_index = (self.frame_index + 1) % len(frames)
        self.background_label.config(image=frames[self.frame_index])
        self.root.after(SPEAK_FRAME_MS if self.state == BotState.SPEAKING else FRAME_MS,
                        self._update_animation)

    def toggle_hud_visibility(self, event=None) -> None:
        try:
            if self.response_text.winfo_ismapped():
                self.response_text.place_forget()
                self.status_label.place_forget()
            else:
                self.response_text.place(relx=0.5, rely=0.82, anchor=self._tk.S)
                self.status_label.place(relx=0.5, rely=1.0, anchor=self._tk.S, relwidth=1)
        except self._tk.TclError:
            pass

    def _on_key(self, event) -> None:
        action = self.mapper.action_for(event.keysym)
        if action is None or self.controller is None:
            return
        if action == Action.QUIT:
            self._quit()
            return
        self.controller.handle_action(action)

    def _quit(self) -> None:
        if self.controller is not None:
            try:
                self.controller.handle_action(Action.QUIT)
            except Exception:
                log.exception("QUIT handler failed")
        self.close()

    # --- thread-safe API ---

    def set_state(self, state: BotState, message: str = "") -> None:
        def _update():
            if self.state != state:
                self.state = state
                self.frame_index = 0
            if message:
                self.status_var.set(message)
        self._post(_update)

    def show_text(self, text: str, who: str = "bmo") -> None:
        def _update():
            tk = self._tk
            prefix = "You: " if who == "user" else "BMO: "
            self.response_text.config(state=tk.NORMAL)
            self.response_text.insert(tk.END, prefix + text + "\n")
            self.response_text.see(tk.END)
            self.response_text.config(state=tk.DISABLED)
        self._post(_update)

    def _post(self, fn) -> None:
        if self._closed:
            return
        try:
            self.root.after(0, fn)
        except (RuntimeError, self._tk.TclError):
            pass

    def run(self, controller: Controller) -> None:
        self.controller = controller
        try:
            self.root.mainloop()
        finally:
            self._closed = True
            controller.shutdown()

    def close(self) -> None:
        self._closed = True
        try:
            self.root.after(0, self.root.quit)
        except (RuntimeError, self._tk.TclError):
            pass

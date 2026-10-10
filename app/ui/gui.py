"""Tk face GUI. tkinter / PIL.ImageTk are imported lazily so headless boxes work."""
from __future__ import annotations

import logging
import math
import os
import random
import time
from pathlib import Path

from app.config import UIConfig
from app.hardware.input import Action, KeyMapper
from app.ui.states import BotState, Controller

log = logging.getLogger(__name__)

SPEAK_FRAME_MS = 50
FRAME_MS = 500
SVG_FRAME_MS = 33
FRAME_STATS_S = 10.0     # how often the face loop checks its own frame rate
# The touch panel sends stray short taps; only a deliberate hold toggles the text box.
LONG_PRESS_MS = 1000


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
    def __init__(self, cfg: UIConfig, faces_dir: Path, keymap: dict[str, str],
                 svg_dir: Path | None = None):
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
            # -fullscreen needs a window manager; bare X (Pi OS Lite kiosk) has none, so
            # also cover the whole screen ourselves and skip WM decorations.
            self.w, self.h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            self.root.overrideredirect(True)
            self.root.geometry(f"{self.w}x{self.h}+0+0")
            self.root.attributes("-fullscreen", True)
            self.root.focus_force()
        else:
            self.w, self.h = cfg.width, cfg.height
            self.root.geometry(f"{self.w}x{self.h}")

        self._press_job = None
        self.animator = self._load_animator(svg_dir)
        self._face_items: list = []
        self._overlay_items: dict = {}
        self.canvas = self.background_label = None
        if self.animator is not None:
            self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0, bd=0)
            self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
            self.canvas.bind("<ButtonPress-1>", self._on_press)
            self.canvas.bind("<ButtonRelease-1>", self._on_release)
        else:
            self.background_label = tk.Label(self.root, bg="black")
            self.background_label.place(x=0, y=0, relwidth=1, relheight=1)
            self.background_label.bind("<ButtonPress-1>", self._on_press)
            self.background_label.bind("<ButtonRelease-1>", self._on_release)

        self.response_text = tk.Text(
            self.root, height=6, width=60, wrap=tk.WORD, state=tk.DISABLED,
            bg="#ffffff", fg="#000000", font=("Arial", 12))
        self.status_var = tk.StringVar(value="Initializing...")
        self.status_label = ttk.Label(
            self.root, textvariable=self.status_var, background="#2e2e2e", foreground="white")

        self.root.bind("<Key>", self._on_key)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

        if self.animator is not None:
            self.animations = {}
            self._update_face()
        else:
            self.animations = self._load_animations(Path(faces_dir))
            self._update_animation()

    def _load_animator(self, svg_dir):
        if not svg_dir:
            return None
        try:
            from app.ui.face import FaceAnimator
            from app.ui.face_svg import load_faces
            return FaceAnimator(load_faces(Path(svg_dir)))
        except Exception:
            log.exception("SVG faces unavailable in %s, using PNG faces", svg_dir)
            return None

    def _update_face(self) -> None:
        if self._closed:
            return
        start = time.monotonic()
        try:
            if self.animator.tick(start):
                self._draw_face()
        except Exception:
            log.exception("Face animation failed")
        self._frame_stats(start, time.monotonic() - start)
        self.root.after(SVG_FRAME_MS, self._update_face)

    def _frame_stats(self, start: float, cost: float) -> None:
        """Log the face's frame rate when it falls behind (gaps between frames well
        over SVG_FRAME_MS), so a laggy display shows up in the journal with numbers."""
        st = getattr(self, "_fstats", None)
        if st is None:
            st = self._fstats = {"since": start, "last": start, "n": 0, "gap": 0.0,
                                 "cost": 0.0, "worst": 0.0}
        st["gap"] = max(st["gap"], start - st["last"])
        st["last"] = start
        st["n"] += 1
        st["cost"] += cost
        st["worst"] = max(st["worst"], cost)
        span = start - st["since"]
        if span < FRAME_STATS_S:
            return
        fps = st["n"] / span
        if fps < 0.75 * 1000 / SVG_FRAME_MS or st["gap"] > 0.25:
            log.warning("Face rendering slow: %.0f fps, longest gap %.0f ms, draw avg %.1f ms "
                        "max %.0f ms (state %s)", fps, st["gap"] * 1000,
                        st["cost"] / st["n"] * 1000, st["worst"] * 1000, self.state.value)
        self._fstats = None

    def _draw_face(self) -> None:
        a, canvas = self.animator, self.canvas
        ops = a.ops(self.w / 800, self.h / 480)
        bg = "#%02x%02x%02x" % tuple(round(c) for c in a.current.bg)
        if canvas.cget("bg") != bg:
            canvas.configure(bg=bg)
        items = self._face_items
        for i, (kind, coords, color, width) in enumerate(ops):
            if i < len(items) and items[i][0] == kind:
                item = items[i][1]
                canvas.coords(item, *coords)
                if kind == "poly":
                    canvas.itemconfigure(item, fill=color, state="normal")
                else:
                    canvas.itemconfigure(item, fill=color, width=width, state="normal")
                continue
            if i < len(items):
                canvas.delete(items[i][1])
            if kind == "poly":
                item = canvas.create_polygon(*coords, fill=color, outline="")
            else:
                item = canvas.create_line(*coords, fill=color, width=width,
                                          capstyle="round", joinstyle="round")
            if i < len(items):
                items[i] = (kind, item)
            else:
                items.append((kind, item))
        for kind, item in items[len(ops):]:
            canvas.itemconfigure(item, state="hidden")
        self._draw_overlay(time.monotonic())

    def _draw_overlay(self, now: float) -> None:
        """Notes, lyrics, equalizer and status caption, drawn above the face."""
        a, canvas = self.animator, self.canvas
        sx, sy = self.w / 800, self.h / 480
        if not self._overlay_items:
            font = ("DejaVu Sans", max(18, int(40 * sy)), "bold")
            self._overlay_items = {
                "notes": [canvas.create_text(0, 0, text=t, font=font, fill="#1d3b2a",
                                             state="hidden") for t in ("\u266a", "\u266b", "\u266a")],
                "bars": [canvas.create_rectangle(0, 0, 0, 0, fill="#1d3b2a", outline="",
                                                 state="hidden") for _ in range(9)],
                "lyric": canvas.create_text(0, 0, font=("DejaVu Sans", max(14, int(26 * sy)), "bold"),
                                            fill="#1d3b2a", width=int(self.w * 0.9),
                                            justify="center", state="hidden"),
                "caption": canvas.create_text(0, 0, font=("DejaVu Sans", max(12, int(24 * sy))),
                                              fill="#1d3b2a", state="hidden"),
            }
        o = self._overlay_items
        on = a.overlay is not None
        for i, item in enumerate(o["notes"]):
            if not on:
                canvas.itemconfigure(item, state="hidden")
                continue
            x = (110 + 290 * i) * sx
            y = (90 + 22 * math.sin(now * 3.2 + i * 2.1)) * sy
            canvas.coords(item, x, y)
            canvas.itemconfigure(item, state="normal")
        music = a.overlay == "music"
        level = a.speech_level(now) if music else 0.0
        for i, item in enumerate(o["bars"]):
            if not music:
                canvas.itemconfigure(item, state="hidden")
                continue
            h = (10 + 70 * level * (0.55 + 0.45 * abs(math.sin(now * 7 + i * 1.3)))) * sy
            x0 = (220 + i * 42) * sx
            canvas.coords(item, x0, self.h - 12 * sy - h, x0 + 30 * sx, self.h - 12 * sy)
            canvas.itemconfigure(item, state="normal")
        lyric = a.lyric(now) if a.overlay == "song" else ""
        canvas.coords(o["lyric"], self.w / 2, self.h - 40 * sy)
        canvas.itemconfigure(o["lyric"], text=lyric, state="normal" if lyric else "hidden")
        canvas.coords(o["caption"], self.w / 2, 34 * sy)
        canvas.itemconfigure(o["caption"], text=a.caption,
                             state="normal" if a.caption else "hidden")
        for group in o.values():
            for item in group if isinstance(group, list) else (group,):
                canvas.tag_raise(item)

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

    def _on_press(self, event=None) -> None:
        self._on_release()
        self._press_job = self.root.after(LONG_PRESS_MS, self._on_long_press)

    def _on_release(self, event=None) -> None:
        if self._press_job is not None:
            self.root.after_cancel(self._press_job)
            self._press_job = None

    def _on_long_press(self) -> None:
        self._press_job = None
        self.toggle_hud_visibility()

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
            if self.animator is not None:
                self.animator.set_state(state, time.monotonic())
            if message:
                self.status_var.set(message)
        self._post(_update)

    def prepare_speech(self, wav_path) -> None:
        """Called just before SPEAKING with the reply WAV, so the mouth can follow it."""
        if self.animator is None:
            return
        from app.ui.face import envelope_from_wav
        env, window = envelope_from_wav(wav_path)
        self._post(lambda: self.animator.prepare_speech(env, window))

    def set_emotion(self, name: str | None) -> None:
        """Show an emotion ('happy', 'sad', ...) on the idle/speaking face for a few seconds."""
        if self.animator is not None:
            self._post(lambda: self.animator.set_emotion(name, time.monotonic()))

    def play_expression(self, name: str) -> float:
        """Run a server expression action (dance, wink, or a face name) on the SVG face.
        Returns its length in seconds (0.0 if this face set can't show it)."""
        if self.animator is None:
            return 0.0
        from app.ui.face import FACE_ACTION_S, emotion_names, move_for
        found = move_for(name)
        if found:
            seconds = found[1].seconds
        elif name in emotion_names(self.animator.faces):
            seconds = FACE_ACTION_S
        else:
            log.info("No expression %r on this face set", name)
            return 0.0
        self._post(lambda: self.animator.play_expression(name, time.monotonic()))
        return seconds

    def show_face(self, name: str) -> float:
        """A server face action: hold that emotion for about a second. Returns the seconds."""
        if self.animator is None:
            return 0.0
        from app.ui.face import FACE_ACTION_S, emotion_names
        if name not in emotion_names(self.animator.faces):
            return 0.0
        self._post(lambda: self.animator.show_face(name, time.monotonic()))
        return FACE_ACTION_S

    def set_overlay(self, kind: str | None, lyrics=()) -> None:
        """Music notes over the face while BMO sings ('song', with lyrics) or plays 'music'."""
        if self.animator is not None:
            lines = list(lyrics)
            self._post(lambda: self.animator.set_overlay(kind, lines))

    def set_caption(self, text: str) -> None:
        """Status text over the face while BMO works ("Searching memories...")."""
        def _update():
            self.status_var.set(text)
            if self.animator is not None:
                self.animator.set_caption(text)
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

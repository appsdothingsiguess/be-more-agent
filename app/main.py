"""Command line entry point: python -m app [--headless | --gui | --text "..." | --self-test]."""

from __future__ import annotations

import argparse
import atexit
import logging
import signal
import sys
import threading

from app.config import ConfigError, TokenError, load_config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app", description="BMO thin client for the home AI server")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true",
                      help="no display: console/stdin + HID buttons (default when no display)")
    mode.add_argument("--gui", action="store_true", help="fullscreen Tk face GUI (needs X/Wayland)")
    mode.add_argument("--text", metavar="MESSAGE", help="send one text message, speak the reply, exit")
    mode.add_argument("--self-test", action="store_true", help="run the physical acceptance diagnostics")
    p.add_argument("--config", help="config file (default: config.json in the repo, if present)")
    p.add_argument("--no-speak", action="store_true", help="with --text: print the reply only")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def build_components(cfg):
    from app.hardware.camera import Camera
    from app.hardware.microphone import Microphone
    from app.hardware.speaker import Speaker
    from app.server.client import BMOClient
    from app.server.reservation import Reservation

    runtime = cfg.runtime_path
    runtime.mkdir(parents=True, exist_ok=True)
    client = BMOClient(cfg)
    reservation = Reservation(client, cfg)
    mic = Microphone(cfg.microphone, runtime)
    speaker = Speaker(cfg.speaker, cfg.path(cfg.sounds.dir))
    camera = Camera(cfg.camera, runtime) if cfg.camera.enabled else None
    return client, reservation, mic, speaker, camera


def _start_evdev(cfg, controller):
    """Feed USB HID keyboards (e.g. the Feather buttons) into the controller."""
    if not cfg.input.evdev_enabled:
        return None
    from pathlib import Path

    from app.hardware.input import EvdevReader, KeyMapper, discover_keyboards

    paths = discover_keyboards() if cfg.input.evdev_device == "auto" else [Path(cfg.input.evdev_device)]
    if not paths:
        logging.getLogger(__name__).info("No HID keyboards found under /dev/input/by-id")
        return None
    reader = EvdevReader(paths, KeyMapper(cfg.input.keymap), controller.handle_action)
    reader.start()
    return reader


def run_interactive(cfg, use_gui: bool) -> int:
    from app.controller import InteractionController

    client, reservation, mic, speaker, camera = build_components(cfg)
    if use_gui:
        from app.ui.gui import TkUI
        ui = TkUI(cfg.ui, cfg.path(cfg.ui.faces_dir), cfg.input.keymap)
    else:
        from app.ui.headless import HeadlessUI
        ui = HeadlessUI()

    controller = InteractionController(cfg, ui, client, reservation, mic, speaker, camera)
    atexit.register(controller.shutdown)
    stop = threading.Event()

    def on_signal(signum, _frame):
        logging.getLogger(__name__).info("Signal %s: shutting down", signum)
        controller.shutdown()
        stop.set()
        if use_gui:
            ui.close()
        elif sys.stdin.isatty():
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    # Hardware buttons via evdev. In GUI mode Tk receives the keys itself.
    reader = None if use_gui else _start_evdev(cfg, controller)
    threading.Thread(target=controller.start, name="bmo-warmup", daemon=True).start()
    try:
        if use_gui or sys.stdin.isatty():
            ui.run(controller)
        else:
            # Service mode (systemd): no stdin; buttons only. Wait for SIGTERM.
            stop.wait()
    finally:
        if reader is not None:
            reader.stop()
        controller.shutdown()
    return 0


def run_text(cfg, message: str, speak: bool) -> int:
    from app.server.client import BMOClient
    from app.server.reservation import Reservation

    client = BMOClient(cfg)
    reservation = Reservation(client, cfg)
    try:
        reservation.ensure_ready()
        result = client.interact(text=message, speak=speak)
        reservation.renewed()
        print(f"BMO: {result.text}")
        if speak and result.audio_wav:
            from app.hardware.speaker import Speaker
            out = cfg.runtime_path
            out.mkdir(parents=True, exist_ok=True)
            reply = out / "reply-text.wav"
            reply.write_bytes(result.audio_wav)
            Speaker(cfg.speaker, cfg.path(cfg.sounds.dir)).play(reply, block=True)
    finally:
        reservation.release()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # urllib3 debug logs include nothing secret, but keep them quiet.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        cfg = load_config(args.config)
        if args.self_test:
            from app.diagnostics import run_diagnostics
            return run_diagnostics(cfg)
        if args.text is not None:
            return run_text(cfg, args.text, speak=not args.no_speak)
        if args.gui:
            from app.ui.gui import gui_available
            if not gui_available():
                print("No display available (no X/Wayland). Use --headless.", file=sys.stderr)
                return 2
            return run_interactive(cfg, use_gui=True)
        if args.headless or not cfg.ui.enabled:
            return run_interactive(cfg, use_gui=False)
        from app.ui.gui import gui_available
        return run_interactive(cfg, use_gui=gui_available())
    except (ConfigError, TokenError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

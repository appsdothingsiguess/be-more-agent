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


def use_stream_backend(cfg) -> bool:
    """Stream (shared always-on capture) vs file recording. Auto = stream when deps exist."""
    backend = cfg.microphone.backend
    if backend == "stream":
        return True
    if backend == "auto":
        import importlib.util
        return (importlib.util.find_spec("openwakeword") is not None
                and importlib.util.find_spec("onnxruntime") is not None)
    return False


def build_live(cfg, mic):
    """Stream backend: returns (capture, StreamMicrophone); listener is built after the controller."""
    from app.audio.capture import AudioCapture
    from app.hardware.stream_mic import StreamMicrophone

    capture = AudioCapture(cfg, publish=None)
    capture.on_device_change = mic.use_device  # replugged mic: redo gain/AGC on the new card
    return capture, StreamMicrophone(cfg, capture, file_mic=mic)


def _make_listener(cfg, controller, capture):
    from app.audio.vad import make_vad
    from app.audio.wakeword import WakeDetector
    from app.config import APP_ROOT
    from app.live import LiveListener

    return LiveListener(cfg, controller, capture,
                        detector_factory=lambda: WakeDetector(cfg.wake_word, APP_ROOT),
                        vad_factory=lambda: make_vad(cfg.listen))


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


def _start_web(cfg, controller):
    """Local web page (text chat, settings, status). Failure never stops BMO."""
    if not cfg.web.enabled:
        return None
    from app.web.server import WebServer
    try:
        web = WebServer(cfg, controller)
        web.start()
        return web
    except OSError as e:
        logging.getLogger(__name__).warning("Web interface not started: %s", e)
        return None


def run_interactive(cfg, use_gui: bool) -> int:
    from app.controller import InteractionController

    client, reservation, mic, speaker, camera = build_components(cfg)
    if use_gui:
        from app.ui.gui import TkUI
        ui = TkUI(cfg.ui, cfg.path(cfg.ui.faces_dir), cfg.input.keymap,
                  svg_dir=cfg.path(cfg.ui.faces_svg_dir) if cfg.ui.face_style == "svg" else None)
    else:
        from app.ui.headless import HeadlessUI
        ui = HeadlessUI()

    capture = listener = None
    if use_stream_backend(cfg):
        capture, mic = build_live(cfg, mic)
    controller = InteractionController(cfg, ui, client, reservation, mic, speaker, camera)
    if capture is not None:
        capture.publish = controller.publish
        capture.start()
        listener = _make_listener(cfg, controller, capture)
        listener.start()
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
    web = _start_web(cfg, controller)
    threading.Thread(target=controller.start, name="bmo-warmup", daemon=True).start()
    try:
        if use_gui or sys.stdin.isatty():
            ui.run(controller)
        else:
            # Service mode (systemd): no stdin; buttons only. Wait for SIGTERM.
            stop.wait()
    finally:
        if listener is not None:
            listener.stop()
        if capture is not None:
            capture.stop()
        if reader is not None:
            reader.stop()
        if web is not None:
            web.stop()
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
            speaker = Speaker(cfg.speaker, cfg.path(cfg.sounds.dir))
            speaker.configure()
            speaker.play(reply, block=True)
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
        from app.settings import load_settings
        load_settings(cfg)  # changes made from the web page
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

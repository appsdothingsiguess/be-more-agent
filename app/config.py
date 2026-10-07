"""Configuration for the BMO thin client.

Defaults mirror the verified hardware/server setup proven by tools/bmo_test.py.
A JSON file (config.json, see config.example.json) overrides any subset of keys.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_ROOT / "config.json"

VISION_MODES = ("off", "always", "manual")

# Physical BMO credential locations, in lookup order after BMO_TOKEN_FILE
# and the configured token_file.
DEFAULT_TOKEN_FILES = ("/etc/bmo/token", "~/.config/bmo/token")


class ConfigError(ValueError):
    pass


class TokenError(RuntimeError):
    pass


@dataclass
class MicrophoneConfig:
    # "auto" = find the ALSA card whose name contains `match`, else use fallback_device.
    device: str = "auto"
    match: str = "USB PnP Sound Device"
    fallback_device: str = "plughw:1,0"
    # amixer card for gain/AGC; None = derive from the resolved device.
    alsa_card: str | int | None = None
    capture_rate: int = 48000
    upload_rate: int = 16000
    channels: int = 1
    gain_db: float = 18.0
    set_capture_gain: bool = True
    capture_gain: str = "100%"
    auto_gain_control: bool = True
    max_seconds: float = 30.0
    min_seconds: float = 0.3


@dataclass
class SpeakerConfig:
    device: str = "auto"
    match: str = "UACDemoV1.0"
    fallback_device: str = "plughw:2,0"
    # amixer card for volume; None = derive from the resolved device.
    alsa_card: str | int | None = None
    set_volume: bool = True
    volume_control: str = "PCM"
    volume: str = "50%"


@dataclass
class CameraConfig:
    enabled: bool = True
    width: int = 640
    height: int = 480
    rotation: int = 0
    vision_mode: str = "always"
    timeout_seconds: float = 30.0


@dataclass
class UIConfig:
    enabled: bool = True
    width: int = 800
    height: int = 480
    fullscreen: bool = True
    faces_dir: str = "faces"


@dataclass
class SoundsConfig:
    enabled: bool = True
    dir: str = "sounds"
    greeting: bool = True
    ack: bool = True
    thinking: bool = True


@dataclass
class InputConfig:
    # Key name (Tk keysym / evdev KEY_* name without prefix, lowercase) -> action.
    # Actions: up, down, left, right, a, b, start, quit.
    keymap: dict[str, str] = field(default_factory=lambda: {
        "return": "start",
        "enter": "start",
        "space": "start",
        "up": "up",
        "down": "down",
        "left": "left",
        "right": "right",
        "a": "a",
        "z": "a",
        "b": "b",
        "x": "b",
        "escape": "quit",
        "esc": "quit",
    })
    # Read HID keyboards (e.g. the Feather) directly from /dev/input when headless.
    evdev_enabled: bool = True
    # "auto" = every /dev/input/by-id/*-event-kbd; or an explicit event device path.
    evdev_device: str = "auto"


@dataclass
class WakeWordConfig:
    enabled: bool = False
    model: str = "wakeword.onnx"
    threshold: float = 0.5


@dataclass
class Config:
    server_url: str = "http://192.168.0.240:8765"
    token_file: str | None = "/etc/bmo/token"
    model: str = "bmo-qwen3-vl-8b"
    connect_timeout: float = 5.0
    request_timeout: float = 900.0
    readiness_timeout: float = 300.0
    readiness_poll_seconds: float = 3.0
    # Reservation lifetime is 300 s server-side; renew before reuse after this long.
    reservation_renew_after: float = 240.0
    runtime_dir: str = "runtime"

    microphone: MicrophoneConfig = field(default_factory=MicrophoneConfig)
    speaker: SpeakerConfig = field(default_factory=SpeakerConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    sounds: SoundsConfig = field(default_factory=SoundsConfig)
    input: InputConfig = field(default_factory=InputConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)

    def path(self, value: str) -> Path:
        """Resolve a config path relative to the project root."""
        p = Path(value).expanduser()
        return p if p.is_absolute() else APP_ROOT / p

    @property
    def runtime_path(self) -> Path:
        return self.path(self.runtime_dir)


def _merge(obj: Any, data: dict[str, Any], prefix: str = "") -> None:
    fields = {f.name: f for f in dataclasses.fields(obj)}
    for key, value in data.items():
        if key.startswith("_"):
            continue  # allow "_comment" style keys
        if key not in fields:
            log.warning("Unknown config key ignored: %s%s", prefix, key)
            continue
        current = getattr(obj, key)
        if dataclasses.is_dataclass(current):
            if not isinstance(value, dict):
                raise ConfigError(f"{prefix}{key} must be an object")
            _merge(current, value, f"{prefix}{key}.")
        elif key == "keymap" and isinstance(value, dict):
            current.update({str(k).lower(): str(v).lower() for k, v in value.items()})
        else:
            setattr(obj, key, value)


def validate(cfg: Config) -> Config:
    if not str(cfg.server_url).startswith(("http://", "https://")):
        raise ConfigError("server_url must start with http:// or https://")
    cfg.server_url = str(cfg.server_url).rstrip("/")
    if cfg.camera.vision_mode not in VISION_MODES:
        raise ConfigError(f"camera.vision_mode must be one of {VISION_MODES}")
    if cfg.camera.rotation not in (0, 90, 180, 270):
        raise ConfigError("camera.rotation must be 0, 90, 180 or 270")
    mic = cfg.microphone
    for name in ("capture_rate", "upload_rate", "channels"):
        if int(getattr(mic, name)) <= 0:
            raise ConfigError(f"microphone.{name} must be positive")
    actions = {"up", "down", "left", "right", "a", "b", "start", "quit"}
    bad = {k: v for k, v in cfg.input.keymap.items() if v not in actions}
    if bad:
        raise ConfigError(f"input.keymap has unknown actions: {bad}")
    return cfg


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load defaults, overlay JSON from `path` (or config.json if present), then env.

    Env overrides: BMO_URL / BMO_SERVER_URL, BMO_MIC_DEVICE, BMO_SPEAKER_DEVICE,
    BMO_MIC_GAIN_DB (same names as tools/bmo_test.py).
    """
    cfg = Config()
    explicit = path is not None
    file = Path(path).expanduser() if explicit else DEFAULT_CONFIG_PATH
    if file.is_file():
        try:
            data = json.loads(file.read_text())
        except json.JSONDecodeError as e:
            raise ConfigError(f"{file}: invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"{file}: top level must be an object")
        _merge(cfg, data)
    elif explicit:
        raise ConfigError(f"Config file not found: {file}")

    env = os.environ
    url = env.get("BMO_SERVER_URL") or env.get("BMO_URL")
    if url:
        cfg.server_url = url
    if env.get("BMO_MIC_DEVICE"):
        cfg.microphone.device = env["BMO_MIC_DEVICE"]
    if env.get("BMO_SPEAKER_DEVICE"):
        cfg.speaker.device = env["BMO_SPEAKER_DEVICE"]
    if env.get("BMO_MIC_GAIN_DB"):
        cfg.microphone.gain_db = float(env["BMO_MIC_GAIN_DB"])
    return validate(cfg)


# ------------------------------------------------------------------
# Physical BMO credential
# ------------------------------------------------------------------

def token_candidates(cfg: Config | None = None) -> list[Path]:
    raw = [os.environ.get("BMO_TOKEN_FILE"), cfg.token_file if cfg else None, *DEFAULT_TOKEN_FILES]
    out: list[Path] = []
    for c in raw:
        if c:
            p = Path(c).expanduser()
            if p not in out:
                out.append(p)
    return out


def find_token_file(cfg: Config | None = None) -> Path | None:
    for p in token_candidates(cfg):
        try:
            if p.is_file() and os.access(p, os.R_OK):
                return p
        except OSError:
            pass
    return None


def read_token(cfg: Config | None = None) -> str:
    """Return the device token. Never log or print the return value."""
    p = find_token_file(cfg)
    if p is None:
        checked = ", ".join(str(c) for c in token_candidates(cfg))
        raise TokenError(f"No readable BMO credential found (checked: {checked}; or set BMO_TOKEN_FILE)")
    token = p.read_text().strip()
    if not token:
        raise TokenError(f"BMO credential file is empty: {p}")
    log.info("Loaded BMO credential from %s (contents not shown)", p)
    return token

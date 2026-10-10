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
TLS_MODES = ("off", "self_signed", "files")
MIC_BACKENDS = ("auto", "file", "stream")
VAD_MODES = ("silero", "energy")

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
    # "auto" = stream when the wake word is enabled, else record to a file.
    backend: str = "auto"
    stream_rate: int = 16000


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
    # "manual" = B button arms the camera for one turn (it faces a wall at the moment).
    vision_mode: str = "manual"
    timeout_seconds: float = 30.0


@dataclass
class UIConfig:
    enabled: bool = True
    width: int = 800
    height: int = 480
    fullscreen: bool = True
    faces_dir: str = "faces"
    # "svg" = animated vector face drawn from faces_svg_dir; "png" = the older still frames.
    face_style: str = "svg"
    faces_svg_dir: str = "faces_svg"
    # Mute: replies and errors are text only (screen / web page); the speaker stays silent.
    text_only: bool = False


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
    })
    # Read HID keyboards (e.g. the Feather) directly from /dev/input when headless.
    evdev_enabled: bool = True
    # "auto" = every /dev/input/by-id/*-event-kbd; or an explicit event device path.
    evdev_device: str = "auto"


@dataclass
class WebConfig:
    # Local web page (text chat, settings, status), LAN only, PIN protected.
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    pin_file: str = "~/.config/bmo/web_pin"
    # HTTPS (needed for browser microphone access): off | self_signed | files.
    tls: str = "off"
    tls_port: int = 8443
    http_redirect: bool = True
    cert_file: str = "~/.config/bmo/tls/cert.pem"
    key_file: str = "~/.config/bmo/tls/key.pem"
    tls_hostnames: list[str] = field(default_factory=list)
    trusted_proxies: list[str] = field(default_factory=list)
    public_origins: list[str] = field(default_factory=list)
    max_voice_bytes: int = 3_000_000


@dataclass
class WakeWordConfig:
    enabled: bool = False
    model: str = "hey_jarvis"
    # Used automatically instead of `model` when the file exists (relative to the project root).
    custom_model: str = "models/hey_bmo.onnx"
    threshold: float = 0.5
    cooldown_seconds: float = 2.0


@dataclass
class ListenConfig:
    # Hands-free listening: end-of-speech detection and follow-up window after a reply.
    vad: str = "silero"  # falls back to energy when silero is unavailable
    end_silence_seconds: float = 0.9
    no_speech_timeout: float = 6.0
    followup: bool = True
    followup_seconds: float = 5.0
    followup_min_speech_seconds: float = 0.15  # shorter sounds don't end a follow-up
    preroll_seconds: float = 0.4
    auto_stop_button: bool = True
    mute_tail_seconds: float = 0.6
    prewarm_on_wake: bool = True


@dataclass
class MemoryConfig:
    # Short-term conversation history sent with each turn, plus server long-term recall.
    enabled: bool = True
    max_messages: int = 10
    max_chars: int = 6000
    file: str = "memory.json"  # relative to runtime_dir
    session_idle_minutes: float = 30.0  # idle time that ends a session and saves it to long-term memory; 0 = never


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
    web: WebConfig = field(default_factory=WebConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    listen: ListenConfig = field(default_factory=ListenConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)

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
    if mic.backend not in MIC_BACKENDS:
        raise ConfigError(f"microphone.backend must be one of {MIC_BACKENDS}")
    if int(mic.stream_rate) <= 0:
        raise ConfigError("microphone.stream_rate must be positive")
    web = cfg.web
    if web.tls not in TLS_MODES:
        raise ConfigError(f"web.tls must be one of {TLS_MODES}")
    if not 1 <= int(web.tls_port) <= 65535:
        raise ConfigError("web.tls_port must be 1-65535")
    if int(web.max_voice_bytes) <= 0:
        raise ConfigError("web.max_voice_bytes must be positive")
    for name in ("tls_hostnames", "trusted_proxies", "public_origins"):
        value = getattr(web, name)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"web.{name} must be a list of strings")
    if not 0 <= float(cfg.wake_word.threshold) <= 1:
        raise ConfigError("wake_word.threshold must be 0-1")
    if float(cfg.wake_word.cooldown_seconds) < 0:
        raise ConfigError("wake_word.cooldown_seconds must not be negative")
    listen = cfg.listen
    if listen.vad not in VAD_MODES:
        raise ConfigError(f"listen.vad must be one of {VAD_MODES}")
    for name in ("end_silence_seconds", "no_speech_timeout", "followup_seconds"):
        if float(getattr(listen, name)) <= 0:
            raise ConfigError(f"listen.{name} must be positive")
    for name in ("preroll_seconds", "mute_tail_seconds", "followup_min_speech_seconds"):
        if float(getattr(listen, name)) < 0:
            raise ConfigError(f"listen.{name} must not be negative")
    if not 2 <= int(cfg.memory.max_messages) <= 10:
        raise ConfigError("memory.max_messages must be 2-10")
    if int(cfg.memory.max_chars) <= 0:
        raise ConfigError("memory.max_chars must be positive")
    if float(cfg.memory.session_idle_minutes) < 0:
        raise ConfigError("memory.session_idle_minutes must not be negative")
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

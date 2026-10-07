"""User-adjustable settings (web page, later buttons), persisted to runtime/settings.json.

Only the keys in ``SETTINGS`` can be changed at runtime. The file is overlaid
on the loaded config at startup; config.json itself is never rewritten.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from app.config import VISION_MODES, Config

log = logging.getLogger(__name__)


class SettingError(ValueError):
    pass


def _volume(value: Any) -> str:
    try:
        n = int(round(float(str(value).strip().rstrip("%"))))
    except (TypeError, ValueError):
        raise SettingError("volume must be a percentage 0-100") from None
    if not 0 <= n <= 100:
        raise SettingError("volume must be a percentage 0-100")
    return f"{n}%"


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise SettingError("must be true or false")


def _vision(value: Any) -> str:
    if value not in VISION_MODES:
        raise SettingError(f"must be one of {', '.join(VISION_MODES)}")
    return value


def _gain(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise SettingError("mic boost must be a number of decibels") from None
    if not 0 <= n <= 30:
        raise SettingError("mic boost must be 0-30 dB")
    return n


# dotted config path -> validator returning the normalised value
SETTINGS: dict[str, Callable[[Any], Any]] = {
    "speaker.volume": _volume,
    "ui.text_only": _bool,
    "camera.vision_mode": _vision,
    "sounds.enabled": _bool,
    "microphone.gain_db": _gain,
}


def settings_path(cfg: Config) -> Path:
    return cfg.runtime_path / "settings.json"


def get_setting(cfg: Config, key: str) -> Any:
    section, name = key.split(".")
    return getattr(getattr(cfg, section), name)


def current(cfg: Config) -> dict[str, Any]:
    return {key: get_setting(cfg, key) for key in SETTINGS}


def set_setting(cfg: Config, key: str, value: Any) -> Any:
    """Validate and apply one setting to cfg (not persisted). Returns the stored value."""
    if key not in SETTINGS:
        raise SettingError(f"unknown setting: {key}")
    value = SETTINGS[key](value)
    section, name = key.split(".")
    setattr(getattr(cfg, section), name, value)
    return value


def load_settings(cfg: Config, path: Path | None = None) -> None:
    """Overlay saved settings onto cfg; invalid or unknown entries are ignored."""
    path = Path(path) if path else settings_path(cfg)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        log.warning("Ignoring unreadable settings file %s: %s", path, e)
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        try:
            set_setting(cfg, key, value)
        except SettingError as e:
            log.warning("Ignoring saved setting %s: %s", key, e)


def save_settings(cfg: Config, path: Path | None = None) -> None:
    path = Path(path) if path else settings_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
        with os.fdopen(fd, "w") as f:
            json.dump(current(cfg), f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Could not save settings to %s: %s", path, e)

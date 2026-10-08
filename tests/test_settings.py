import json

import pytest
import requests

from app.config import Config
from app.server.client import BMOClient
from app.server.errors import RequestCancelled, ServerBusy, ServerUnavailable
from app.settings import (SETTINGS, SettingError, current, load_settings, save_settings,
                          set_setting, settings_path)


def make_cfg(tmp_path):
    return Config(runtime_dir=str(tmp_path / "runtime"))


def test_set_validates_and_normalises(tmp_path):
    cfg = make_cfg(tmp_path)
    assert set_setting(cfg, "speaker.volume", 70) == "70%"
    assert set_setting(cfg, "speaker.volume", "35%") == "35%"
    assert set_setting(cfg, "microphone.gain_db", "12") == 12.0
    assert set_setting(cfg, "ui.text_only", True) is True
    assert set_setting(cfg, "camera.vision_mode", "off") == "off"
    for key, bad in [("speaker.volume", 150), ("speaker.volume", "loud"), ("ui.text_only", "yes"),
                     ("camera.vision_mode", "sometimes"), ("microphone.gain_db", 99),
                     ("token_file", "/x"), ("server_url", "http://evil")]:
        with pytest.raises(SettingError):
            set_setting(cfg, key, bad)
    assert cfg.server_url == "http://192.168.0.240:8765"


def test_save_and_load_roundtrip(tmp_path):
    cfg = make_cfg(tmp_path)
    set_setting(cfg, "speaker.volume", 30)
    set_setting(cfg, "ui.text_only", True)
    save_settings(cfg)
    data = json.loads(settings_path(cfg).read_text())
    assert set(data) == set(SETTINGS)
    fresh = make_cfg(tmp_path)
    load_settings(fresh)
    assert current(fresh) == current(cfg)


def test_load_ignores_bad_entries(tmp_path):
    cfg = make_cfg(tmp_path)
    p = settings_path(cfg)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"speaker.volume": "999%", "token_file": "/evil", "ui.text_only": True}))
    load_settings(cfg)
    assert cfg.speaker.volume == "50%" and cfg.ui.text_only is True
    assert cfg.token_file == "/etc/bmo/token"
    p.write_text("{broken")
    load_settings(cfg)  # warns only


def _response(status, body, ctype="application/json"):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode() if not isinstance(body, bytes) else body
    r.headers["content-type"] = ctype
    return r


def test_scheduler_unavailable_maps_to_server_busy():
    r = _response(503, {"status": "unavailable", "code": "large_model_session_active",
                        "display_message": "I'm unavailable while this server is running a large AI model."})
    with pytest.raises(ServerBusy) as e:
        BMOClient._raise_for(r)
    assert e.value.code == "large_model_session_active"
    assert "large AI model" in e.value.display_message and e.value.status == 503


def test_unavailable_cancel_code_and_plain_errors():
    with pytest.raises(RequestCancelled):
        BMOClient._raise_for(_response(409, {"status": "unavailable", "code": "request_cancelled"}))
    with pytest.raises(ServerUnavailable):
        BMOClient._raise_for(_response(503, b"oops", "text/plain"))


def test_memory_enabled_setting(tmp_path):
    cfg = make_cfg(tmp_path)
    assert "memory.enabled" in SETTINGS
    assert set_setting(cfg, "memory.enabled", False) is False
    assert cfg.memory.enabled is False
    with pytest.raises(SettingError):
        set_setting(cfg, "memory.enabled", "off")


def test_wake_and_followup_settings(tmp_path):
    cfg = make_cfg(tmp_path)
    assert set_setting(cfg, "wake_word.enabled", True) is True
    assert set_setting(cfg, "listen.followup", False) is False
    assert set_setting(cfg, "listen.followup_seconds", "8") == 8.0
    for key, bad in [("wake_word.enabled", "yes"), ("listen.followup", 1),
                     ("listen.followup_seconds", 1), ("listen.followup_seconds", 16),
                     ("listen.followup_seconds", "soon"), ("listen.followup_seconds", True)]:
        with pytest.raises(SettingError):
            set_setting(cfg, key, bad)
    assert current(cfg)["listen.followup_seconds"] == 8.0


def test_session_idle_minutes_setting():
    cfg = Config()
    assert set_setting(cfg, "memory.session_idle_minutes", 0) == 0
    assert set_setting(cfg, "memory.session_idle_minutes", 7.5) == 7.5
    for bad in (-1, 121, "x", True, None):
        with pytest.raises(SettingError):
            set_setting(cfg, "memory.session_idle_minutes", bad)

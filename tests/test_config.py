import json

import pytest

from app import config as C


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("BMO_URL", "BMO_SERVER_URL", "BMO_MIC_DEVICE", "BMO_SPEAKER_DEVICE",
              "BMO_MIC_GAIN_DB", "BMO_TOKEN_FILE"):
        monkeypatch.delenv(k, raising=False)


def test_defaults_match_verified_hardware(tmp_path):
    cfg = C.load_config(C.APP_ROOT / "config.example.json")
    assert cfg.server_url == "http://192.168.0.240:8765"
    assert cfg.model == "bmo-qwen3-vl-8b"
    assert cfg.microphone.fallback_device == "plughw:1,0"
    assert cfg.microphone.capture_rate == 48000
    assert cfg.microphone.upload_rate == 16000
    assert cfg.microphone.gain_db == 18
    assert cfg.speaker.fallback_device == "plughw:2,0"
    assert (cfg.camera.width, cfg.camera.height, cfg.camera.vision_mode) == (640, 480, "manual")
    assert cfg.wake_word.enabled is False


def test_partial_override_and_nested_merge(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "server_url": "http://example:1/",
        "microphone": {"device": "plughw:3,0"},
        "input": {"keymap": {"F1": "start"}},
        "_comment": "ignored",
        "bogus": 1,
    }))
    cfg = C.load_config(p)
    assert cfg.server_url == "http://example:1"  # trailing slash stripped
    assert cfg.microphone.device == "plughw:3,0"
    assert cfg.microphone.gain_db == 18  # untouched default
    assert cfg.input.keymap["f1"] == "start"
    assert cfg.input.keymap["return"] == "start"  # defaults kept


@pytest.mark.parametrize("data", [
    {"server_url": "ftp://x"},
    {"camera": {"vision_mode": "sometimes"}},
    {"camera": {"rotation": 45}},
    {"input": {"keymap": {"q": "explode"}}},
    {"microphone": "plughw:1,0"},
])
def test_invalid_config_rejected(tmp_path, data):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data))
    with pytest.raises(C.ConfigError):
        C.load_config(p)


def test_missing_explicit_config_is_error(tmp_path):
    with pytest.raises(C.ConfigError):
        C.load_config(tmp_path / "nope.json")


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("BMO_URL", "http://100.84.254.7:8765")
    monkeypatch.setenv("BMO_SPEAKER_DEVICE", "plughw:5,0")
    cfg = C.load_config(C.APP_ROOT / "config.example.json")
    assert cfg.server_url == "http://100.84.254.7:8765"
    assert cfg.speaker.device == "plughw:5,0"


def test_token_lookup_order_and_env(monkeypatch, tmp_path):
    env_tok = tmp_path / "env_token"
    env_tok.write_text("env-secret\n")
    cfg_tok = tmp_path / "cfg_token"
    cfg_tok.write_text("cfg-secret")
    cfg = C.Config(token_file=str(cfg_tok))
    assert C.read_token(cfg) == "cfg-secret"
    monkeypatch.setenv("BMO_TOKEN_FILE", str(env_tok))
    assert C.find_token_file(cfg) == env_tok
    assert C.read_token(cfg) == "env-secret"


def test_token_missing_or_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "DEFAULT_TOKEN_FILES", ())
    cfg = C.Config(token_file=str(tmp_path / "absent"))
    with pytest.raises(C.TokenError):
        C.read_token(cfg)
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    with pytest.raises(C.TokenError) as e:
        C.read_token(C.Config(token_file=str(empty)))
    assert "empty" in str(e.value)


def test_token_never_logged(monkeypatch, tmp_path, caplog):
    tok = tmp_path / "t"
    tok.write_text("super-secret-value")
    caplog.set_level("DEBUG")
    C.read_token(C.Config(token_file=str(tok)))
    assert "super-secret-value" not in caplog.text

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


def test_memory_defaults_and_validation(tmp_path):
    cfg = C.load_config(C.APP_ROOT / "config.example.json")
    assert cfg.memory.enabled and cfg.memory.max_messages == 10 and cfg.memory.max_chars == 6000
    assert cfg.memory.file == "memory.json"
    for bad in ({"max_messages": 1}, {"max_messages": 11}, {"max_chars": 0}):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"memory": bad}))
        with pytest.raises(C.ConfigError):
            C.load_config(p)

def test_web_mic_wake_listen_defaults():
    cfg = C.load_config(C.APP_ROOT / "config.example.json")
    w = cfg.web
    assert (w.tls, w.tls_port, w.http_redirect, w.max_voice_bytes) == ("off", 8443, True, 3_000_000)
    assert w.tls_hostnames == [] and w.trusted_proxies == [] and w.public_origins == []
    assert w.cert_file.endswith("tls/cert.pem") and w.key_file.endswith("tls/key.pem")
    assert (cfg.microphone.backend, cfg.microphone.stream_rate) == ("auto", 16000)
    wk = cfg.wake_word
    assert (wk.enabled, wk.model, wk.custom_model, wk.threshold, wk.cooldown_seconds) == (
        False, "hey_jarvis", "models/hey_bmo.onnx", 0.5, 2.0)
    ln = cfg.listen
    assert (ln.vad, ln.end_silence_seconds, ln.no_speech_timeout) == ("silero", 0.9, 6.0)
    assert (ln.followup, ln.followup_seconds, ln.preroll_seconds) == (True, 5.0, 0.4)
    assert (ln.auto_stop_button, ln.mute_tail_seconds, ln.prewarm_on_wake) == (True, 0.6, True)


def test_old_wake_word_keys_still_load(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"wake_word": {"enabled": True, "model": "x.onnx", "threshold": 0.7}}))
    cfg = C.load_config(p)
    assert cfg.wake_word.enabled and cfg.wake_word.model == "x.onnx"
    assert cfg.wake_word.threshold == 0.7 and cfg.wake_word.cooldown_seconds == 2.0


def test_new_sections_validated(tmp_path):
    bad = [{"web": {"tls": "maybe"}}, {"web": {"tls_port": 0}}, {"web": {"max_voice_bytes": 0}},
           {"web": {"trusted_proxies": "10.0.0.1"}}, {"microphone": {"backend": "usb"}},
           {"microphone": {"stream_rate": 0}}, {"wake_word": {"threshold": 2}},
           {"listen": {"vad": "magic"}}, {"listen": {"end_silence_seconds": 0}},
           {"listen": {"preroll_seconds": -1}}]
    for section in bad:
        p = tmp_path / "c.json"
        p.write_text(json.dumps(section))
        with pytest.raises(C.ConfigError):
            C.load_config(p)


def test_audio_stream_off_by_default_and_env_switch(monkeypatch, tmp_path):
    monkeypatch.delenv("BMO_AUDIO_STREAM", raising=False)
    assert C.load_config(C.APP_ROOT / "config.example.json").audio_stream is False
    monkeypatch.setenv("BMO_AUDIO_STREAM", "1")
    assert C.load_config(C.APP_ROOT / "config.example.json").audio_stream is True
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"audio_stream": True}))
    monkeypatch.setenv("BMO_AUDIO_STREAM", "0")
    assert C.load_config(p).audio_stream is False      # the env wins over the file
    monkeypatch.setenv("BMO_AUDIO_STREAM", "maybe")
    with pytest.raises(C.ConfigError):
        C.load_config(p)

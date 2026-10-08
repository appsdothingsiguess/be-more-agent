import base64
import json
import logging
import threading
import uuid

import pytest

from app.config import Config
from app.server import (
    AuthError,
    BadResponse,
    BMOClient,
    ServerUnavailable,
)
from tests.fake_bmo_server import FakeBMOServer

TOKEN = "s3cret-token-value-xyz"


@pytest.fixture
def server():
    s = FakeBMOServer().start()
    yield s
    s.stop()


@pytest.fixture
def client(server):
    cfg = Config(server_url=server.url, connect_timeout=2.0, request_timeout=10.0)
    return BMOClient(cfg, token=TOKEN, session_id="sess-1")


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "in.wav"
    p.write_bytes(b"RIFF....WAVE")
    return str(p)


@pytest.fixture
def jpg(tmp_path):
    p = tmp_path / "pic.jpg"
    p.write_bytes(b"\xff\xd8\xff")
    return str(p)


def test_health_no_auth(client, server):
    assert client.health() == {"status": "ok"}
    h = server.requests[-1]["headers"]
    assert "authorization" not in h


def test_authed_headers(client, server):
    client.status()
    client.models()
    for r in server.requests:
        assert r["headers"]["authorization"] == f"Bearer {TOKEN}"
        assert r["headers"]["x-session-id"] == "sess-1"
    assert client.models() == ["bmo-qwen3-vl-8b", "other"]


def test_distinct_request_ids(client, server, wav):
    client.chat([{"role": "user", "content": "x"}])
    client.speech("hi")
    client.transcribe(wav)
    client.interact(text="hi")
    ids = [r["headers"]["x-request-id"] for r in server.requests]
    assert len(ids) == 4 and len(set(ids)) == 4
    for i in ids:
        uuid.UUID(i)


def test_interact_fields(client, server, wav, jpg):
    res = client.interact(audio_path=wav, image_path=jpg, speak=True)
    rec = server.requests[-1]
    assert rec["fields"] == {"speak": None, "audio": "in.wav", "image": "pic.jpg"}
    assert b"true" in rec["form_body"]
    assert res.text == "hi there" and res.transcript == "hello"
    assert res.audio_wav.startswith(b"RIFF")
    assert "audio_wav_base64" not in res.raw
    assert res.request_id == rec["headers"]["x-request-id"]
    client.interact(text="yo", speak=False)
    rec = server.requests[-1]
    assert set(rec["fields"]) == {"speak", "text"}
    assert b"false" in rec["form_body"]


def test_interact_requires_input(client):
    with pytest.raises(ValueError):
        client.interact()


def test_interact_bad_base64(client, server):
    server.interact_audio_b64 = "!!!not base64!!!"
    with pytest.raises(BadResponse):
        client.interact(text="hi")


def test_transcribe_field_file(client, server, wav):
    assert client.transcribe(wav) == "transcribed"
    assert server.requests[-1]["fields"] == {"file": "in.wav"}


def test_speech_wav(client, server):
    assert client.speech("hello").startswith(b"RIFF")
    assert server.requests[-1]["json"] == {"input": "hello"}


def test_chat_payload(client, server):
    assert client.chat([{"role": "user", "content": "x"}], max_tokens=5) == "chat reply"
    j = server.requests[-1]["json"]
    assert j["model"] == "bmo-qwen3-vl-8b" and j["max_tokens"] == 5


def test_reserve_release(client, server):
    assert client.reserve() == 200
    client.release()
    assert server.requests[-1]["method"] == "DELETE"


def test_auth_error(client, server):
    server.auth_mode_401 = True
    with pytest.raises(AuthError):
        client.status()


def test_connection_refused():
    cfg = Config(server_url="http://127.0.0.1:1", connect_timeout=1.0)
    with pytest.raises(ServerUnavailable):
        BMOClient(cfg, token=TOKEN).status()


def test_cancel_while_interact_blocked(client, server):
    server.interact_delay = 10
    rid = BMOClient.new_request_id()
    out = {}

    def run():
        out["res"] = client.interact(text="slow", request_id=rid)

    t = threading.Thread(target=run)
    t.start()
    assert server.interact_started.wait(5)
    assert client.cancel(rid) is True
    t.join(5)
    assert not t.is_alive()
    assert server.find("POST", f"/v1/requests/{rid}/cancel")


def test_cancel_failure_returns_false(client, server):
    server.cancel_status = 404
    assert client.cancel("abc") is False


def test_token_not_leaked(client, caplog):
    caplog.set_level(logging.DEBUG)
    client.status()
    client.cancel("abc")
    assert TOKEN not in repr(client)
    assert TOKEN not in caplog.text


def test_interact_history_and_memory_fields(client, server):
    hist = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    client.interact(text="yo", history=hist, memory=True)
    vals = server.requests[-1]["values"]
    assert json.loads(vals["history"]) == hist and vals["memory"] == "on"
    client.interact(text="yo", history=[], memory=False)
    vals = server.requests[-1]["values"]
    assert "history" not in vals and vals["memory"] == "off"
    client.interact(text="yo")
    assert set(server.requests[-1]["values"]) == {"speak", "text"}


def test_memory_routes(client, server):
    assert client.list_memories(limit=5, offset=2)[0]["content"] == "likes tea"
    assert server.requests[-1]["query"] == "limit=5&offset=2"
    assert client.delete_memory(1) is True
    assert client.delete_memory(1) is None
    assert client.forget_memories() == 0


def test_memory_routes_404_means_unavailable(client, server):
    server.memory_routes = False
    assert client.list_memories() is None
    assert client.forget_memories() is None
    assert client.delete_memory(3) is None


def test_memory_routes_other_errors_raise(client, server):
    server.auth_mode_401 = True
    with pytest.raises(AuthError):
        client.list_memories()


@pytest.mark.parametrize("sent,expected", [
    ("excited", "excited"), ("chewing", "chewing"), (None, "neutral"), ("", "neutral"),
    ("furious", "neutral"), (7, "neutral"), (["happy"], "neutral")])
def test_emotion_field_is_validated(server, client, sent, expected):
    server.interact_extra = {"emotion": sent}
    assert client.interact(text="hi").emotion == expected


def test_missing_emotion_from_older_server_is_neutral(client):
    assert client.interact(text="hi").emotion == "neutral"


def test_all_server_emotions_exist_as_faces():
    from app.server.client import EMOTIONS
    from app.ui.face import emotion_names
    from app.ui.face_svg import load_faces
    faces = load_faces("faces_svg")
    assert EMOTIONS - {"neutral"} <= set(faces) and set(emotion_names(faces)) <= EMOTIONS

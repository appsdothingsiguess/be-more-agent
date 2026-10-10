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
    ServerBusy,
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
    assert client.list_memories(limit=5, offset=2)[0]["name"] == "likes-tea"
    assert server.requests[-1]["query"] == "limit=5&offset=2"
    assert client.delete_memory("likes-tea") is True
    assert server.requests[-1]["path"] == "/v1/bmo/memories/likes-tea"
    assert client.delete_memory("likes-tea") is None
    assert client.forget_memories() == 0


def test_memory_routes_404_means_unavailable(client, server):
    server.memory_routes = False
    assert client.list_memories() is None
    assert client.forget_memories() is None
    assert client.delete_memory("x") is None


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


def test_end_session(client, server):
    assert client.end_session("abc") is True
    rec = server.requests[-1]
    assert rec["method"] == "POST" and rec["path"] == "/v1/bmo/session/end"
    assert rec["headers"]["x-session-id"] == "abc" and rec["headers"]["authorization"]
    assert client.session_id == "sess-1"          # only this request used the other id
    server.session_ended = False
    assert client.end_session("abc") is False


def test_end_session_404_and_errors(client, server):
    server.memory_routes = False
    assert client.end_session("abc") is None
    server.memory_routes = True
    for code in (403, 405):                       # v1 server: no session route for BMO
        server.session_end_status = code
        assert client.end_session("abc") is None
    server.session_end_status = 200
    server.auth_mode_401 = True
    with pytest.raises(AuthError):
        client.end_session("abc")


def test_actions_and_statuses_are_parsed(server, client):
    import base64
    wav = base64.b64encode(b"RIFFcoin").decode()
    server.interact_extra = {
        "actions": [{"type": "expression", "name": "dance"},
                    {"type": "sound", "name": "coin", "audio_wav_base64": wav},
                    {"type": "sound", "name": "broken", "audio_wav_base64": "!!"},
                    {"type": "sound", "name": "silent"},
                    {"type": "teleport", "name": "x"}, {"type": "expression"}, "junk"],
        "statuses": ["Searching memories...", {"text": "Thinking"}, "", 3]}
    r = client.interact(text="hi")
    assert [(a.type, a.name) for a in r.actions] == [("expression", "dance"), ("sound", "coin")]
    assert r.actions[1].audio_wav == b"RIFFcoin"
    assert r.statuses == ("Searching memories...", "Thinking")
    assert "audio_wav_base64" not in r.raw["actions"][1]


def test_older_server_has_no_actions(client):
    r = client.interact(text="hi")
    assert r.actions == () and r.statuses == ()


def test_face_music_song_and_memory_changes_are_parsed(server, client):
    server.interact_extra = {
        "actions": [{"type": "face", "name": "surprised"}, {"type": "music", "name": "dance_party"},
                    {"type": "face"}],
        "song": {"mood": "silly"},
        "memory_changes": [{"op": "create", "name": "dog-finn"}, {"op": "explode"}, "x"]}
    r = client.interact(text="hi")
    assert [(a.type, a.name) for a in r.actions] == [("face", "surprised"), ("music", "dance_party")]
    assert r.actions[1].audio_wav is None
    assert r.song == "silly"
    assert r.memory_changes == ({"op": "create", "name": "dog-finn"},)


@pytest.mark.parametrize("sent,expected", [(4.2, 4.2), (3, 3.0), (0, 0.0), (-1, None),
                                           ("4.2", None), (True, None)])
def test_music_start_is_parsed(server, client, sent, expected):
    server.interact_extra = {"music_start_s": sent, "brand_new_field": {"x": 1},
                             "actions": [{"type": "hologram", "name": "x"},
                                         {"type": "music", "name": "dance_party"}]}
    r = client.interact(text="hi")
    assert r.music_start_s == expected
    assert [a.type for a in r.actions] == ["music"]       # unknown types are ignored


def test_older_server_has_no_music_start(client):
    assert client.interact(text="hi").music_start_s is None


def _stream_server(server):
    wav = base64.b64encode(b"RIFFcoin").decode()
    server.interact_extra = {"emotion": "happy", "song": {"mood": "happy"},
                             "actions": [{"type": "expression", "name": "wink"},
                                         {"type": "sound", "name": "coin",
                                          "audio_wav_base64": wav}]}
    server.stream_events = [
        {"type": "transcript", "text": "hello"},
        {"type": "status", "status": "searching_memories", "text": "Searching memories...",
         "face": "thinking", "expression": "look_around"},
        {"type": "expression", "name": "wink"},
        {"type": "sound", "name": "coin", "audio_wav_base64": wav},
        {"type": "song", "mood": "happy"},
    ]


def test_stream_events_in_order_and_result(server, client):
    _stream_server(server)
    seen = []
    r = client.interact(text="hi", on_event=seen.append)
    assert server.find("POST", "/v1/bmo/interact")[-1]["values"]["stream"] == "true"
    kinds = [e["type"] if e["type"] != "action" else e["action"].type for e in seen]
    assert kinds == ["transcript", "status", "expression", "sound", "song"]
    assert seen[1]["text"] == "Searching memories..."
    # Sounds keep the audio from the stream (the result repeats them without it).
    assert [(a.type, a.name) for a in r.actions] == [("expression", "wink"), ("sound", "coin")]
    assert r.actions[1].audio_wav == b"RIFFcoin"
    assert r.text == "hi there" and r.emotion == "happy" and r.song == "happy"
    assert r.audio_wav and "type" not in r.raw


def test_no_stream_field_without_callback(server, client):
    _stream_server(server)
    r = client.interact(text="hi")
    assert "stream" not in server.find("POST", "/v1/bmo/interact")[-1]["values"]
    assert r.actions[1].audio_wav == b"RIFFcoin"


def test_stream_falls_back_to_json_on_older_server(server, client):
    seen = []
    r = client.interact(text="hi", on_event=seen.append)
    assert r.text == "hi there" and seen == []


@pytest.mark.parametrize("event,exc", [
    ({"type": "error", "status": 504, "detail": "deadline"}, ServerUnavailable),
    ({"type": "error", "status": 503, "code": "large_model_session_active", "detail": "Tiel"},
     ServerBusy),
    ({"type": "error", "status": 422, "detail": "bad"}, BadResponse),
])
def test_stream_error_event(server, client, event, exc):
    server.stream_events = [{"type": "status", "text": "x"}, event]
    with pytest.raises(exc):
        client.interact(text="hi", on_event=lambda e: None)


def test_stream_without_result_is_bad_response(server, client):
    server.stream_events = []
    server.interact_extra = {}

    def route(rec, orig=server._route):
        status, payload, raw = orig(rec)
        return (status, None, raw.split(b"data: {\"type\": \"result\"")[0] or b"data: {}\n\n") \
            if raw else (status, payload, raw)
    server._route = route
    with pytest.raises(BadResponse):
        client.interact(text="hi", on_event=lambda e: None)


def test_callback_exception_aborts(server, client):
    _stream_server(server)

    class Stop(Exception):
        pass

    def cb(e):
        raise Stop()
    with pytest.raises(Stop):
        client.interact(text="hi", on_event=cb)


def test_plain_503_detail_is_busy(server, client):
    server._route = lambda rec: (503, {"detail": "large_model_session_active"}, None)
    with pytest.raises(ServerBusy) as e:
        client.interact(text="hi")
    assert e.value.code == "large_model_session_active"


def test_put_memory(server, client):
    topic = client.put_memory("favorite-food", "preference", "Loves pizza.")
    assert topic["name"] == "favorite-food"
    assert server.find("PUT", "/v1/bmo/memories/favorite-food")[-1]["json"] == {
        "type": "preference", "content": "Loves pizza."}
    with pytest.raises(BadResponse) as e:
        client.put_memory("x", "fact", " ")
    assert e.value.status == 422 and "sentences" in e.value.detail
    server.memory_routes = False
    assert client.put_memory("x", "fact", "Hi.") is None

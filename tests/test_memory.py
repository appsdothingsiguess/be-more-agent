import json
import os

import pytest

from app.memory import (ConversationMemory, ConversationSession, is_forget_command,
                        is_goodbye, is_new_session_command)


def test_missing_file_is_empty(tmp_path):
    assert len(ConversationMemory(tmp_path / "m.json")) == 0


def test_add_persists_and_reloads(tmp_path):
    p = tmp_path / "m.json"
    m = ConversationMemory(p)
    m.add_exchange("  hi ", " hello ")
    assert m.messages() == [{"role": "user", "content": "hi"},
                            {"role": "assistant", "content": "hello"}]
    assert ConversationMemory(p).messages() == m.messages()


def test_messages_returns_copy(tmp_path):
    m = ConversationMemory(tmp_path / "m.json")
    m.add_exchange("a", "b")
    m.messages()[0]["content"] = "x"
    m.messages().clear()
    assert m.messages()[0]["content"] == "a"


def test_empty_sides_ignored(tmp_path):
    m = ConversationMemory(tmp_path / "m.json")
    m.add_exchange("hi", "  ")
    m.add_exchange("", "hello")
    assert len(m) == 0 and not (tmp_path / "m.json").exists()


@pytest.mark.parametrize("content", ["{not json", "{}", '"str"', "[1, 2]"])
def test_corrupt_file_starts_empty(tmp_path, content, caplog):
    p = tmp_path / "m.json"
    p.write_text(content)
    assert len(ConversationMemory(p)) == 0


def test_invalid_items_dropped(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps([
        {"role": "assistant", "content": "orphan"},
        {"role": "system", "content": "bad"},
        {"role": "user", "content": 5},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]))
    assert ConversationMemory(p).messages() == [{"role": "user", "content": "q"},
                                                {"role": "assistant", "content": "a"}]


def test_trim_by_count_keeps_whole_pairs(tmp_path):
    m = ConversationMemory(tmp_path / "m.json", max_messages=4)
    for i in range(5):
        m.add_exchange(f"q{i}", f"a{i}")
    msgs = m.messages()
    assert [x["content"] for x in msgs] == ["q3", "a3", "q4", "a4"]
    assert msgs[0]["role"] == "user"


def test_trim_by_chars(tmp_path):
    m = ConversationMemory(tmp_path / "m.json", max_chars=20)
    m.add_exchange("a" * 8, "b" * 8)
    m.add_exchange("c" * 3, "d" * 3)
    assert [x["content"][0] for x in m.messages()] == ["c", "d"]
    m.add_exchange("e" * 30, "f")  # a single oversized pair drops everything
    assert m.messages() == []


def test_load_trims_to_limits(tmp_path):
    p = tmp_path / "m.json"
    big = ConversationMemory(p)
    for i in range(5):
        big.add_exchange(f"q{i}", f"a{i}")
    assert len(ConversationMemory(p, max_messages=2)) == 2


def test_clear(tmp_path):
    p = tmp_path / "m.json"
    m = ConversationMemory(p)
    m.add_exchange("a", "b")
    m.clear()
    assert len(m) == 0 and json.loads(p.read_text()) == []


def test_atomic_save_leaves_no_temp_files(tmp_path):
    m = ConversationMemory(tmp_path / "m.json")
    m.add_exchange("a", "b")
    assert os.listdir(tmp_path) == ["m.json"]


def test_save_error_is_logged_not_raised(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    m = ConversationMemory(blocker / "m.json")
    m.add_exchange("a", "b")
    assert len(m) == 2


@pytest.mark.parametrize("text", [
    "forget everything", "Forget Everything.", "BMO, forget everything!",
    "reset memory please", "bmo reset memory, please", "  FORGET   everything  ",
])
def test_forget_commands(text):
    assert is_forget_command(text)


@pytest.mark.parametrize("text", [
    "", "don't forget everything", "forget everything about cats", "please forget everything",
    "can you forget everything", "reset", "do not reset memory", "bmo bmo forget everything",
])
def test_not_forget_commands(text):
    assert not is_forget_command(text)


# -- sessions ---------------------------------------------------------------
def test_session_persists_and_reloads(tmp_path):
    p = tmp_path / "s.json"
    s = ConversationSession(p)
    assert s.is_empty and len(s.id) == 32
    s.add_exchange(" hi ", "hello")
    data = json.loads(p.read_text())
    assert data["id"] == s.id and data["messages"][0] == {"role": "user", "content": "hi"}
    again = ConversationSession(p)
    assert again.id == s.id and again.messages() == s.messages() and not again.is_empty
    assert again.started_at == s.started_at


def test_session_caps_drop_oldest_pairs(tmp_path):
    s = ConversationSession(tmp_path / "s.json")
    for i in range(50):
        s.add_exchange(f"q{i}", f"a{i}")
    msgs = s.messages()
    assert len(msgs) == 80 and msgs[0] == {"role": "user", "content": "q10"}
    big = ConversationSession(tmp_path / "b.json")
    for i in range(10):
        big.add_exchange("q" * 5000, "a" * 5000)
    assert sum(len(m["content"]) for m in big.messages()) <= 40000 and len(big) == 8


def test_session_idle_and_reset(tmp_path):
    t = [100.0]
    s = ConversationSession(tmp_path / "s.json", clock=lambda: t[0])
    s.add_exchange("q", "a")
    t[0] = 160.0
    assert s.idle_seconds() == 60 and s.idle_seconds(now=400.0) == 300
    old = s.id
    assert s.reset() != old and s.is_empty
    assert ConversationSession(tmp_path / "s.json").is_empty


def test_session_bad_file_ignored(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("[1,2")
    assert ConversationSession(p).is_empty
    p.write_text('{"id": 5}')
    assert ConversationSession(p).is_empty


@pytest.mark.parametrize("text", ["new session", "New conversation!", "start over", "bmo, new chat",
                                  "Hey BMO, start a new conversation please", "new session please"])
def test_new_session_command_matches(text):
    assert is_new_session_command(text)


@pytest.mark.parametrize("text", ["", "what is a new session", "tell me about a new chat",
                                  "forget everything", "start overnight"])
def test_new_session_command_rejects(text):
    assert not is_new_session_command(text)


@pytest.mark.parametrize("text", ["Goodbye!", "bye BMO", "Okay, thanks BMO. Bye!", "That's all for now.",
                                  "See you later, BMO", "Good night!", "thank you, that's it",
                                  "Alright bye bye"])
def test_goodbye_matches(text):
    assert is_goodbye(text)


@pytest.mark.parametrize("text", ["", "thank you", "how do you say goodbye in french",
                                  "bye the way what time is it", "say goodbye to grandma", "okay"])
def test_goodbye_rejects(text):
    assert not is_goodbye(text)

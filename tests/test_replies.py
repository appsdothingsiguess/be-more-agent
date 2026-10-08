import os

import pytest

from app.replies import ReplyStore


def test_new_id_is_16_hex():
    a, b = ReplyStore.new_id(), ReplyStore.new_id()
    assert len(a) == 16 and int(a, 16) >= 0 and a != b


def test_save_and_path(tmp_path):
    store = ReplyStore(tmp_path / "replies")
    tid = store.new_id()
    assert store.path(tid) is None
    saved = store.save(tid, b"wav")
    assert store.path(tid) == saved and saved.read_bytes() == b"wav"


def test_path_rejects_bad_ids(tmp_path):
    store = ReplyStore(tmp_path / "replies")
    store.save("0123456789abcdef", b"x")
    for bad in ("../0123456789abcdef", "0123456789ABCDEF", "0123456789abcde", "0123456789abcdef0",
                "", "..", "0123456789abcdef.wav"):
        assert store.path(bad) is None
    with pytest.raises(ValueError):
        store.save("../evil", b"x")


def test_prune_keeps_newest(tmp_path):
    store = ReplyStore(tmp_path / "replies", keep=3)
    ids = [f"{i:016x}" for i in range(5)]
    for i, tid in enumerate(ids):
        p = store.save(tid, b"x")
        os.utime(p, (1000 + i, 1000 + i))
        store.prune()
    assert [store.path(t) is not None for t in ids] == [False, False, True, True, True]


def test_purge_legacy(tmp_path):
    (tmp_path / "reply-1.wav").write_bytes(b"x")
    (tmp_path / "reply-text.wav").write_bytes(b"x")
    (tmp_path / "keep.json").write_text("{}")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "a.wav").write_bytes(b"x")
    (tmp_path / "replies").mkdir()
    (tmp_path / "replies" / "0123456789abcdef.wav").write_bytes(b"x")
    ReplyStore.purge_legacy(tmp_path)
    assert not list(tmp_path.glob("reply-*.wav")) and not list((tmp_path / "uploads").iterdir())
    assert (tmp_path / "keep.json").exists()
    assert (tmp_path / "replies" / "0123456789abcdef.wav").exists()
    ReplyStore.purge_legacy(tmp_path / "missing")  # no error

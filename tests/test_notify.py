import wave
from pathlib import Path

from app.notify import ERRORS, NOTICES, SERVER_CODES, SOFT, clip_path, spoken_text

SOUNDS = Path(__file__).resolve().parent.parent / "sounds"


def test_every_code_has_a_valid_clip():
    for code in ERRORS:
        path = clip_path(SOUNDS, code)
        assert path.is_file(), f"missing clip for {code}; run tools/make_error_clips.py"
        with wave.open(str(path)) as w:
            seconds = w.getnframes() / w.getframerate()
        assert 1.0 < seconds < 12.0, code


def test_catalogue_consistency():
    assert set(SERVER_CODES.values()) <= set(ERRORS)
    assert (SOFT | NOTICES) <= set(ERRORS)
    for code, info in ERRORS.items():
        assert info.message and info.hint
        assert spoken_text(code).startswith(info.message)

from pathlib import Path

from app.ui.states import TRANSITIONS, BotState, can_transition

FACES = Path(__file__).resolve().parent.parent / "faces"


def test_table_complete():
    assert set(TRANSITIONS) == set(BotState)


def test_can_transition_samples():
    assert can_transition(BotState.WARMUP, BotState.IDLE)
    assert can_transition(BotState.SPEAKING, BotState.SPEAKING)
    assert can_transition(BotState.ERROR, BotState.WARMUP)
    assert not can_transition(BotState.WARMUP, BotState.SPEAKING)
    assert not can_transition(BotState.IDLE, BotState.SPEAKING)


def test_values_match_faces_dirs():
    dirs = {p.name for p in FACES.iterdir() if p.is_dir()}
    assert {s.value for s in BotState} == dirs

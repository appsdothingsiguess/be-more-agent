import math
import random
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from app.ui import face, face_svg
from app.ui.states import BotState

SVG_DIR = Path(__file__).resolve().parent.parent / "faces_svg"
NAMES = {"neutral", "happy", "excited", "sad", "surprised", "confused", "angry", "sleepy",
         "thinking", "listening", "error", "blink", "mouth_closed", "mouth_small",
         "mouth_open", "mouth_wide", "mouth_o", "mouth_ee", "mouth_pucker", "mouth_tall", "mouth_teeth"}


@pytest.fixture(scope="module")
def faces():
    return face_svg.load_faces(SVG_DIR)


def test_all_expressions_load(faces):
    assert NAMES <= set(faces)
    for name, f in faces.items():
        assert f.bg == (194, 222, 172)
        for shapes in f.parts.values():
            for s in shapes:
                assert len(s.pts) == 2 * face_svg.POINTS, name


def test_part_ids_are_normalised(faces):
    assert "mouth" in faces["mouth_wide"].parts            # mouth-wide -> mouth
    assert {"tongue", "teeth"} <= set(faces["mouth_wide"].parts)
    assert {"eye-left", "eye-right"} <= set(faces["neutral"].parts)
    assert len(faces["error"].parts["eye-left"]) == 2      # the X is two strokes
    assert faces["blank"].parts == {}


def test_blink_is_a_flat_line_and_neutral_eye_a_filled_circle(faces):
    eye = faces["neutral"].parts["eye-left"][0]
    assert eye.fill[3] == 1 and eye.stroke[3] == 0
    xs, ys = eye.pts[0::2], eye.pts[1::2]
    assert min(xs) == pytest.approx(264, abs=0.5) and max(ys) - min(ys) == pytest.approx(32, abs=0.5)
    blink = faces["blink"].parts["eye-left"][0]
    assert blink.fill[3] == 0 and blink.stroke[3] == 1 and blink.width == 10
    assert max(blink.pts[1::2]) - min(blink.pts[1::2]) == pytest.approx(0, abs=0.01)


def test_path_parser_commands():
    sub = face_svg.parse_path("M 0 0 L 10 0 H 20 V 10 Z")
    assert sub == [([(0, 0), (10, 0), (20, 0), (20, 10)], True)]
    rel = face_svg.parse_path("m 5 5 l 5 0 l 0 5")
    assert rel[0][0][-1] == (10, 10) and rel[0][1] is False
    q = face_svg.parse_path("M 0 0 Q 5 10 10 0 T 20 0")      # T mirrors the control point
    assert q[0][0][-1] == (20, 0)
    assert min(y for _, y in q[0][0]) < -4                   # second arc bows the other way
    two = face_svg.parse_path("M 0 0 L 1 1 M 5 5 L 6 6")
    assert len(two) == 2


def test_bad_input_is_rejected():
    with pytest.raises(ValueError):
        face_svg.parse_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 480">'
                           '<path id="mouth" d="M 0 0 L 1 1" stroke="red"/></svg>')


def test_approach_and_snap(faces):
    a, b = faces["neutral"].copy(), faces["happy"].copy()
    face_svg.pad_pair(a, b)
    face_svg.snap(a, b)
    assert a.parts["mouth"][0].pts == b.parts["mouth"][0].pts
    c = faces["neutral"].copy()
    face_svg.pad_pair(c, b)
    moved = [face_svg.approach(c, b, 0.5) for _ in range(40)]
    assert moved[0] > moved[-1] and moved[-1] < 0.01


def test_pad_pair_adds_invisible_parts(faces):
    a, b = faces["neutral"].copy(), faces["excited"].copy()
    face_svg.pad_pair(a, b)
    assert set(a.parts) == set(b.parts)
    tongue = a.parts["tongue"][0]
    assert tongue.fill[3] == 0 and tongue.width == 0


def test_draw_ops_order_and_offsets(faces):
    ops = face_svg.draw_ops(faces["happy"], 1, 1)
    assert [k for k, *_ in ops] == ["poly", "poly", "poly"]    # mouth, two eyes
    shifted = face_svg.draw_ops(faces["happy"], 1, 1, {"eye-left": (10, 5)})
    assert shifted[1][1][0] == pytest.approx(ops[1][1][0] + 10)
    wide = face_svg.draw_ops(faces["mouth_wide"], 1, 1)
    colors = [c for _, _, c, _ in wide]
    assert colors.index("#f16972") < colors.index("#ffffff")   # tongue then teeth over mouth
    strokes = [o for o in face_svg.draw_ops(faces["neutral"], 2, 2) if o[0] == "line"]
    assert strokes and strokes[0][3] == pytest.approx(16)      # 8 wide at 2x scale


def _wav(path, samples, rate=8000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def test_envelope_follows_loudness(tmp_path):
    loud = [int(20000 * math.sin(i / 3)) for i in range(4000)]
    quiet = [0] * 4000
    _wav(tmp_path / "a.wav", loud + quiet + loud)
    env, window = face.envelope_from_wav(tmp_path / "a.wav")
    assert window == face.ENV_WINDOW_S
    n = len(env)
    assert env[2] > 0.8 and env[n // 2] == 0 and env[-3] > 0.8
    assert face.envelope_from_wav(tmp_path / "missing.wav")[0] == []
    (tmp_path / "bad.wav").write_bytes(b"nope")
    assert face.envelope_from_wav(tmp_path / "bad.wav")[0] == []


def test_mouth_levels():
    assert [face.mouth_for_level(x) for x in (0, 0.2, 0.4, 0.9)] == \
        ["mouth_closed", "mouth_small", "mouth_open", "mouth_wide"]


def run(anim, start, seconds, step=0.033):
    t = start
    while t < start + seconds:
        anim.tick(t)
        t += step
    return t


def part_y(f, name="mouth"):
    return sum(f.parts[name][0].pts[1::2]) / face_svg.POINTS


def test_states_choose_expressions(faces):
    a = face.FaceAnimator(faces, random.Random(1))
    t = run(a, 0, 1)
    assert a._target_key[0] == "sleepy"
    for state, expr in [(BotState.IDLE, "neutral"), (BotState.LISTENING, "listening"),
                        (BotState.THINKING, "thinking"), (BotState.ERROR, "error")]:
        a.set_state(state, t)
        t = run(a, t, 0.3)
        assert a._target_key[0] == expr


def test_face_settles_on_target(faces):
    a = face.FaceAnimator(faces, random.Random(1))
    a.set_state(BotState.IDLE, 0)
    t = run(a, 0, 1)
    a.set_state(BotState.ERROR, t)
    t = run(a, t, 3)
    err = faces["error"].parts["mouth"][0].pts
    assert max(abs(p - q) for p, q in zip(a.current.parts["mouth"][0].pts, err)) < 1


def test_blinks_only_on_round_eyes(faces):
    a = face.FaceAnimator(faces, random.Random(2))
    a.set_state(BotState.IDLE, 0)
    t, blinked = 0.0, False
    while t < 12:
        a.tick(t)
        blinked |= a._target_key[2]
        t += 0.033
    assert blinked
    a.set_state(BotState.ERROR, t)
    for _ in range(300):
        t += 0.033
        a.tick(t)
        assert not a._target_key[2]


def test_lipsync_opens_mouth_with_loud_audio(faces, tmp_path):
    a = face.FaceAnimator(faces, random.Random(3))
    a.set_state(BotState.IDLE, 0)
    t = run(a, 0, 1)
    a.prepare_speech([0.0] * 10 + [0.9] * 10 + [0.0] * 10)
    a.set_state(BotState.SPEAKING, t)
    t0 = t
    run(a, t0, 0.15)
    closed_y = part_y(a.current)
    mids = []
    for k in range(60):
        a.tick(t0 + 0.15 + k * 0.01)
        mids.append(a._mouth)
    assert set(mids) & {"mouth_wide", "mouth_tall"}
    run(a, t0 + 0.75, 0.08)                                    # still inside the loud stretch
    assert part_y(a.current) > closed_y + 5                    # wide mouth hangs lower
    assert "tongue" in a.target.parts
    run(a, t0 + 0.9, 1.5)
    assert a._mouth == "mouth_closed"


def test_speaking_without_audio_still_flaps(faces):
    a = face.FaceAnimator(faces, random.Random(4))
    a.set_state(BotState.SPEAKING, 0)
    seen = set()
    t = 0.0
    for _ in range(300):
        a.tick(t)
        seen.add(a._mouth)
        t += 0.033
    assert len(seen) >= 3


def test_emotion_shows_then_fades(faces):
    a = face.FaceAnimator(faces, random.Random(5))
    a.set_state(BotState.IDLE, 0)
    a.set_emotion("happy", 0.5)
    run(a, 0.5, 0.3)
    assert a._target_key[0] == "happy"
    run(a, 1, face.EMOTION_HOLD_S + 1)
    assert a._target_key[0] == "neutral"
    a.set_emotion("nonsense", 20)
    assert a.emotion is None                                    # unknown names are ignored
    a.set_state(BotState.LISTENING, 21)
    run(a, 21, 0.3)
    assert a._target_key[0] == "listening"


def test_idle_eventually_sleeps(faces):
    a = face.FaceAnimator(faces, random.Random(6))
    a.set_state(BotState.IDLE, 0)
    run(a, 0, face.SLEEP_AFTER_S + 5, step=0.5)
    assert a._target_key[0] == "sleepy"
    a.set_state(BotState.LISTENING, 200)
    run(a, 200, 1)
    assert a._target_key[0] == "listening"


def test_eyes_drift_only_when_idle(faces):
    a = face.FaceAnimator(faces, random.Random(7))
    a.set_state(BotState.IDLE, 0)
    run(a, 0, 20)
    assert max(abs(v) for v in a._drift_goal) > 0 or a._drift != (0, 0)
    a.set_state(BotState.ERROR, 20)
    run(a, 20, 4)
    assert abs(a._drift[0]) < 0.5 and abs(a._drift[1]) < 0.5


def test_ops_are_drawable(faces):
    a = face.FaceAnimator(faces, random.Random(8))
    a.set_state(BotState.IDLE, 0)
    run(a, 0, 1)
    ops = a.ops(1.0, 1.0)
    assert ops and all(k in ("poly", "line") and len(c) % 2 == 0 for k, c, *_ in ops)


def test_face_modules_are_tk_free():
    code = "import sys, app.ui.face, app.ui.face_svg; sys.exit('tkinter' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


class FakeCanvas:
    def __init__(self):
        self.items, self.bg, self.next = {}, "#000000", 1

    def cget(self, key):
        return self.bg

    def configure(self, **kw):
        self.bg = kw.get("bg", self.bg)

    def create_polygon(self, *coords, **kw):
        return self._add("poly", coords, kw)

    def create_line(self, *coords, **kw):
        return self._add("line", coords, kw)

    def create_text(self, *coords, **kw):
        return self._add("text", coords, kw)

    def create_rectangle(self, *coords, **kw):
        return self._add("rect", coords, kw)

    def tag_raise(self, i):
        self.raised = getattr(self, "raised", []) + [i]

    def _add(self, kind, coords, kw):
        i, self.next = self.next, self.next + 1
        self.items[i] = {"kind": kind, "coords": coords, "state": "normal", **kw}
        return i

    def coords(self, i, *coords):
        self.items[i]["coords"] = coords

    def itemconfigure(self, i, **kw):
        self.items[i].update(kw)

    def delete(self, i):
        del self.items[i]


def test_gui_draw_face_reuses_and_hides_canvas_items(faces):
    from app.ui import gui
    ui = gui.TkUI.__new__(gui.TkUI)
    ui.w, ui.h = 800, 480
    ui.canvas, ui._face_items, ui._overlay_items = FakeCanvas(), [], {}
    ui.animator = face.FaceAnimator(faces, random.Random(9))
    ui.animator.set_state(BotState.IDLE, 0)
    ui.animator.tick(0)
    ui._draw_face()
    face_kinds = ("poly", "line")
    shown = lambda: [i for i in ui.canvas.items.values()
                     if i["state"] == "normal" and i["kind"] in face_kinds]
    first = len(shown())
    assert first >= 3 and ui.canvas.bg == "#c2deac"
    ids = set(ui.canvas.items)
    ui.animator.set_state(BotState.IDLE, 0.1)
    ui.animator.set_emotion("excited", 0.1)
    for k in range(120):
        ui.animator.tick(0.1 + k * 0.033)
    ui._draw_face()
    assert ids <= set(ui.canvas.items) or len(ui.canvas.items) >= first   # pooled, not leaked
    assert len([i for i in ui.canvas.items.values() if i["kind"] in face_kinds]) <= 12
    ui.animator.set_state(BotState.ERROR, 5)
    for k in range(120):
        ui.animator.tick(5 + k * 0.033)
    ui._draw_face()
    assert all(i["kind"] in ("poly", "line", "text", "rect") for i in ui.canvas.items.values())
    lines = [i for i in ui.canvas.items.values() if i["kind"] == "line" and i["state"] == "normal"]
    assert lines and all(i["capstyle"] == "round" or "capstyle" not in i for i in lines)


def test_emotion_list_for_the_server(faces):
    names = face.emotion_names(faces)
    assert {"neutral", "happy", "excited", "sad", "angry", "kiss", "dizzy", "chewing"} <= set(names)
    assert not any(n.startswith("mouth_") or n in ("blink", "error", "listening") for n in names)
    assert all(face_svg.parse_svg((SVG_DIR / p.name).read_text()) for p in SVG_DIR.glob("2[4-7]*.svg"))


def test_moves_shift_the_face_and_end(faces):
    a = face.FaceAnimator(faces, random.Random(5))
    a.set_state(BotState.SPEAKING, 0)
    assert a.play_expression("Dance", 0.0)
    run(a, 0.0, 0.5)
    dx, dy = a.offsets()["mouth"]
    assert abs(dx) + abs(dy) > 1
    run(a, 0.5, face.MOVES["dance"].seconds)
    assert a._move is None and a.offsets()["mouth"] == (0.0, 0.0)


def test_wink_closes_one_eye(faces):
    a = face.FaceAnimator(faces, random.Random(5))
    a.set_state(BotState.IDLE, 0)
    assert a.play_expression("wink", 0.0)
    a._next_blink = 99
    run(a, 0.0, 0.2)
    assert a._target_key[3] is True
    assert a.target.parts["eye-right"][0].pts == faces["blink"].parts["eye-right"][0].pts
    assert a.target.parts["eye-left"][0].pts != faces["blink"].parts["eye-left"][0].pts


def test_expression_action_face_names_and_unknowns(faces):
    a = face.FaceAnimator(faces, random.Random(5))
    a.set_state(BotState.IDLE, 0)
    assert a.play_expression("kiss", 0.0) and a.emotion == "kiss"
    assert not a.play_expression("moonwalk", 0.0)
    assert not a.play_expression("mouth_open", 0.0)


@pytest.mark.parametrize("name", ["blink", "wink", "laugh", "look_around", "nod", "shake_head",
                                  "bounce", "wiggle", "sparkle_eyes", "heart_eyes", "yawn",
                                  "dance"])
def test_every_server_expression_plays_and_ends(faces, name):
    a = face.FaceAnimator(faces, random.Random(5))
    a.set_state(BotState.IDLE, 0)
    secs = a.play_expression(name, 0.0)
    assert 0.3 <= secs <= 2.0
    run(a, 0.0, secs / 2)
    assert a._move is not None
    run(a, secs / 2, secs + 0.2)
    assert a._move is None and a.offsets()["mouth"] == (0.0, 0.0)


def test_heart_and_star_eyes_replace_the_eyes(faces):
    a = face.FaceAnimator(faces, random.Random(5))
    a.set_state(BotState.IDLE, 0)
    a.play_expression("heart_eyes", 0.0)
    run(a, 0.0, 0.3)
    heart = a.target.parts["eye-left"][0]
    assert len(heart.pts) == 2 * face_svg.POINTS and heart.fill[0] > 200
    assert a._expression(0.3) == "happy"
    a.play_expression("sparkle_eyes", 2.0)
    run(a, 2.0, 2.3)
    assert a.target.parts["eye-right"][0].pts != heart.pts


def test_look_around_moves_only_the_eyes(faces):
    a = face.FaceAnimator(faces, random.Random(5))
    a.set_state(BotState.THINKING, 0)
    a.play_expression("look_around", 0.0)
    off = a.offsets(0.375)
    assert abs(off["eye-left"][0]) > 30 and off["mouth"] == (0.0, 0.0)


def test_face_action_shows_even_while_thinking(faces):
    a = face.FaceAnimator(faces, random.Random(5))
    a.set_state(BotState.THINKING, 0)
    assert a.show_face("surprised", 0.0)
    assert a._expression(0.5) == "surprised"
    assert a._expression(1.2) == "thinking"
    assert not a.show_face("mouth_open", 0.0)


def test_song_overlay_spreads_lyrics_over_the_reply(faces):
    a = face.FaceAnimator(faces, random.Random(5))
    a.set_state(BotState.THINKING, 0)
    a.set_overlay("song", ["La la", "", "Finn is a dog", "The end"])
    a.prepare_speech([0.5] * 150, 0.04)      # 6 s of audio
    a.set_state(BotState.SPEAKING, 10.0)
    assert a.lyric(10.5) == "La la"
    assert a.lyric(13.0) == "Finn is a dog"
    assert a.lyric(30.0) == "The end"
    a.set_state(BotState.IDLE, 31.0)
    assert a.overlay is None and a.lyric(31.0) == ""


def test_caption_clears_when_thinking_ends(faces):
    a = face.FaceAnimator(faces, random.Random(5))
    a.set_state(BotState.THINKING, 0)
    a.set_caption("Searching memories...")
    a.set_state(BotState.SPEAKING, 1.0)
    assert a.caption == ""


def test_gui_overlay_shows_notes_lyrics_and_caption(faces):
    from app.ui import gui
    ui = gui.TkUI.__new__(gui.TkUI)
    ui.w, ui.h = 800, 480
    ui.canvas, ui._face_items, ui._overlay_items = FakeCanvas(), [], {}
    a = ui.animator = face.FaceAnimator(faces, random.Random(9))
    a.set_state(BotState.THINKING, 0)
    a.set_caption("Searching memories...")
    a.tick(0)
    ui._draw_face()
    visible = lambda kind: [i for i in ui.canvas.items.values()
                            if i["kind"] == kind and i["state"] == "normal"]
    assert [i["text"] for i in visible("text")] == ["Searching memories..."]
    a.set_overlay("song", ["Finn is my friend"])
    a.prepare_speech([0.5] * 100)
    a.set_state(BotState.SPEAKING, 1.0)
    ui._draw_face()
    texts = [i["text"] for i in visible("text")]
    assert "Finn is my friend" in texts and len(texts) == 4   # 3 notes + lyric
    assert not visible("rect")
    a.set_overlay("music")
    ui._draw_face()
    assert len(visible("rect")) == 9
    a.set_state(BotState.IDLE, 9.0)
    ui._draw_face()
    assert not visible("text") and not visible("rect")


def test_streamed_song_shows_each_line_at_its_piece(faces):
    a = face.FaceAnimator(faces, random.Random(5))
    a.set_state(BotState.THINKING, 0)
    a.set_overlay("song", ["La la", "Finn is a dog"])
    a.prepare_speech([0.5] * 25, 0.04)       # the opening bar: 1 s
    a.time_lyrics()
    a.set_state(BotState.SPEAKING, 10.0)
    assert a.lyric(10.5) == ""               # music first, no words yet
    a.append_speech([0.5] * 50)
    a.add_lyric_start(1.0)
    a.append_speech([0.5] * 50)
    a.add_lyric_start(3.0)
    a.append_speech([0.1] * 25)              # the outro bar
    a.add_lyric_start(5.0)                   # more pieces than lines: ignored
    assert len(a.envelope) == 150
    assert a.lyric(11.5) == "La la"
    assert a.lyric(13.5) == "Finn is a dog"
    assert a.lyric(15.5) == "Finn is a dog"
    a.set_state(BotState.IDLE, 16.0)
    assert a.lyric_starts is None


def test_speech_appended_before_speaking_joins_the_pending_envelope(faces):
    a = face.FaceAnimator(faces, random.Random(5))
    a.prepare_speech([0.5] * 2, 0.04)
    a.append_speech([0.2] * 3)
    a.set_state(BotState.SPEAKING, 0.0)
    assert a.envelope == [0.5, 0.5, 0.2, 0.2, 0.2]


def test_envelope_from_wav_bytes(tmp_path):
    from tests.wavutil import make_wav
    make_wav(tmp_path / "a.wav", rate=22050, secs=0.4)
    env, window = face.envelope_from_wav((tmp_path / "a.wav").read_bytes())
    assert env and env == face.envelope_from_wav(tmp_path / "a.wav")[0]

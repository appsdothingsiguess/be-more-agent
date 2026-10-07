"""Physical acceptance diagnostics for `python -m app --self-test`.

Reproduces tools/bmo_test.py through the thin-client modules. The token is
never printed.
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

from app.server.errors import BMOError
from app.server.reservation import ReservationState

TOTAL = 15
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class _Skip(Exception):
    """Raised by a step to report SKIP with a reason."""


def _wav_info(path) -> tuple[int, int, float]:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        return rate, w.getnchannels(), (w.getnframes() / rate if rate else 0.0)


def run_diagnostics(cfg, *, components=None, input_fn=input, out=print,
                    record_seconds: float = 5.0, skip_interactive: bool = False,
                    cancel_after: float = 1.5) -> int:
    """Run the 15 acceptance steps. Returns 0 if nothing failed, else 1."""
    if components is None:
        from app.main import build_components
        client, reservation, mic, speaker, camera = build_components(cfg)
    else:
        client = components["client"]
        reservation = components["reservation"]
        mic = components.get("mic")
        speaker = components.get("speaker")
        camera = components.get("camera")

    runtime = Path(cfg.runtime_path)
    runtime.mkdir(parents=True, exist_ok=True)

    results: list[tuple[int, str, str, str]] = []
    ctx: dict = {}
    state = {"blocked": False}  # a SKIP caused by an earlier failure counts as failure

    def step(n, name, fn):
        try:
            detail = fn() or ""
            status = PASS
        except _Skip as e:
            status, detail = SKIP, str(e)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 - diagnostics must continue
            status, detail = FAIL, f"{type(e).__name__}: {e}"
        out(f"[{n}/{TOTAL}] {name} ... {status} ({detail})")
        results.append((n, name, status, detail))

    def need(key, what):
        if ctx.get(key) is None:
            state["blocked"] = True
            raise _Skip(f"{what} unavailable (earlier step failed)")
        return ctx[key]

    def play(path):
        if speaker is None:
            raise _Skip("no speaker")
        return speaker.play(path, block=True)

    def s_devices():
        parts = []
        for label, dev in (("mic", mic), ("speaker", speaker)):
            r = getattr(dev, "resolved", None)
            if r is None:
                parts.append(f"{label}=n/a")
            else:
                parts.append(f"{label}={r.device} ({getattr(r, 'source', '?')})")
        cam = "n/a" if camera is None else ("available" if camera.available() else "NOT available")
        parts.append(f"camera={cam}")
        return "; ".join(parts)

    def s_camera():
        if camera is None:
            raise _Skip("camera disabled")
        p = Path(camera.capture())
        size = p.stat().st_size
        if size <= 1000:
            raise RuntimeError(f"image too small ({size} bytes)")
        ctx["image"] = str(p)
        return f"{p.name}, {size} bytes"

    def s_mic():
        if mic is None:
            raise _Skip("no microphone")
        mic.configure()
        if not skip_interactive:
            input_fn(f"Speak for {record_seconds:g} seconds after pressing Enter... ")
        p = mic.record(record_seconds)
        if not p:
            raise RuntimeError("recording too short or empty")
        rate, ch, dur = _wav_info(p)
        if rate != 16000 or ch != 1:
            raise RuntimeError(f"unexpected format {rate} Hz, {ch} ch (want 16000 Hz mono)")
        ctx["audio"] = str(p)
        gain = getattr(getattr(mic, "cfg", None), "gain_db", "?")
        return f"{dur:.1f}s, 16000 Hz mono, +{gain} dB gain applied"

    def s_speaker():
        audio = need("audio", "recording")
        if not play(audio):
            raise RuntimeError("playback failed")
        if skip_interactive:
            return "played; heard-confirmation skipped"
        ans = str(input_fn("Did you hear yourself? [y/n] ")).strip().lower()
        if not ans.startswith("y"):
            raise RuntimeError("user did not hear playback")
        return "user confirmed"

    def s_health():
        return f"{client.health()}"

    def s_models():
        models = client.models()
        if cfg.model not in models:
            raise RuntimeError(f"model {cfg.model!r} not in {models}")
        detail = f"{cfg.model} listed"
        extra = [m for m in models if m != cfg.model]
        if extra:
            detail += f"; WARNING other models listed: {extra}"
        st = client.status()
        detail += f"; status bmo_ready={st.get('bmo_ready')} mode={st.get('mode')}"
        return detail

    def s_reserve():
        ctx["reserved"] = True
        reservation.ensure_ready()
        if reservation.state != ReservationState.READY:
            raise RuntimeError(f"state {reservation.state}")
        return "state READY"

    def s_chat():
        text = client.chat([{"role": "user", "content": "Say hello in one short sentence."}],
                           max_tokens=60)
        if not text or not text.strip():
            raise RuntimeError("empty reply")
        return text.strip()[:80]

    def s_tts():
        data = client.speech("Hello, I am BMO.")
        p = runtime / "diag_tts.wav"
        p.write_bytes(data)
        if speaker is None:
            return f"{len(data)} bytes (no speaker to play)"
        play(p)
        return f"{len(data)} bytes, played"

    def s_transcribe():
        audio = need("audio", "recording")
        return f"{client.transcribe(audio)!r}"

    def s_vision():
        image = need("image", "camera image")
        r = client.interact(text="Describe what you see in one sentence.",
                            image_path=image, speak=False)
        if not r.text.strip():
            raise RuntimeError("empty description")
        return r.text.strip()[:80]

    def s_full():
        audio = need("audio", "recording")
        r = client.interact(audio_path=audio, image_path=ctx.get("image"), speak=True)
        out(f"      transcript: {r.transcript!r}")
        out(f"      text: {r.text!r}")
        detail = f"{len(r.text)} chars"
        if r.audio_wav:
            p = runtime / "diag_reply.wav"
            p.write_bytes(r.audio_wav)
            play(p)
            detail += ", reply audio played"
        else:
            detail += ", no audio returned"
        reservation.renewed()
        return detail

    def s_cancel():
        rid = client.new_request_id()
        outcome: dict = {}

        def worker():
            try:
                r = client.interact(
                    text="Count slowly from one to one hundred, one number per line.",
                    speak=True, request_id=rid)
                outcome["how"] = f"returned normally ({len(r.text)} chars)"
            except BMOError as e:
                outcome["how"] = f"raised {type(e).__name__}"
            except Exception as e:  # noqa: BLE001
                outcome["how"] = f"raised {type(e).__name__}"

        t = threading.Thread(target=worker, name="diag-interact", daemon=True)
        t.start()
        time.sleep(cancel_after)
        ack = client.cancel(rid)
        t.join(timeout=30)
        if t.is_alive():
            raise RuntimeError(f"interact did not finish after cancel (ack={ack})")
        reservation.mark_stale()
        reservation.ensure_ready()
        if reservation.state != ReservationState.READY:
            raise RuntimeError(f"state {reservation.state} after recovery")
        if not client.status().get("bmo_ready"):
            raise RuntimeError("bmo_ready false after recovery")
        ctx["cancelled_rid"] = rid
        return f"cancel ack={ack}; interact {outcome.get('how')}; recovered READY"

    def s_post_cancel():
        old = ctx.get("cancelled_rid")
        if old is None:
            state["blocked"] = True
            raise _Skip("cancellation step did not complete")
        r = client.interact(text="Say OK.", speak=False)
        if r.request_id == old:
            raise RuntimeError("request id was not new")
        return f"ok, new request id {r.request_id[:8]}"

    def s_release():
        if not ctx.get("reserved"):
            raise _Skip("nothing reserved")
        reservation.release()
        if reservation.state != ReservationState.RELEASED:
            raise RuntimeError(f"state {reservation.state}")
        return "released"

    steps = [
        (1, "devices", s_devices), (2, "camera", s_camera), (3, "microphone", s_mic),
        (4, "speaker", s_speaker), (5, "health", s_health), (6, "auth/discovery", s_models),
        (7, "reservation", s_reserve), (8, "text chat", s_chat), (9, "tts", s_tts),
        (10, "transcription", s_transcribe), (11, "vision", s_vision),
        (12, "full interaction", s_full), (13, "cancellation", s_cancel),
        (14, "post-cancel inference", s_post_cancel),
    ]

    out("BMO self-test (credential contents were NOT printed)")
    interrupted = False
    try:
        for n, name, fn in steps:
            step(n, name, fn)
    except KeyboardInterrupt:
        interrupted = True
        out("Interrupted; releasing reservation")
    finally:
        if not interrupted:
            step(15, "release", s_release)
        else:
            try:
                reservation.release()
                out(f"[15/{TOTAL}] release ... PASS (after interrupt)")
            except Exception as e:  # noqa: BLE001
                out(f"[15/{TOTAL}] release ... FAIL ({type(e).__name__})")

    out("")
    out("Summary")
    out("-------")
    for n, name, status, _detail in results:
        out(f"{n:>2}. {name:<24} {status}")
    failed = [r for r in results if r[2] == FAIL]
    ok = not failed and not state["blocked"] and not interrupted
    out("RESULT: " + ("ALL REQUIRED STEPS PASSED" if ok else f"{len(failed)} step(s) FAILED"))
    return 0 if ok else 1

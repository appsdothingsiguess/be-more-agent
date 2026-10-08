#!/usr/bin/env python3
"""Tune the wake word: print live score + VAD per frame and CPU% over N seconds.

Opens the REAL microphone, so stop the bmo-agent service first:
    sudo systemctl stop bmo-agent
    venv/bin/python tools/wake_test.py --seconds 30
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from app.audio.capture import AudioCapture  # noqa: E402
from app.audio.vad import make_vad  # noqa: E402
from app.audio.wakeword import WakeDetector  # noqa: E402
from app.config import APP_ROOT, load_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config")
    ap.add_argument("--seconds", type=float, default=30.0)
    args = ap.parse_args()
    cfg = load_config(args.config)
    detector = WakeDetector(cfg.wake_word, APP_ROOT)
    vad = make_vad(cfg.listen)
    print(f"model={detector.name} threshold={cfg.wake_word.threshold} vad={type(vad).__name__}")

    done = threading.Event()
    lines = []

    def sink(frame):
        lines.append(frame)

    cap = AudioCapture(cfg, publish=lambda e: print("live:", e))
    cap.add_sink(sink)
    cap.start()
    t0, c0 = time.monotonic(), time.process_time()
    try:
        while time.monotonic() - t0 < args.seconds:
            if not lines:
                done.wait(0.02)
                continue
            frame = lines.pop(0)
            score = detector.score(frame)
            speech = vad.is_speech(frame)
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            flag = " <-- WAKE" if score >= cfg.wake_word.threshold else ""
            print(f"score={score:.3f} vad={int(speech)} rms={rms:7.1f} backlog={len(lines)}{flag}")
    except KeyboardInterrupt:
        pass
    finally:
        cap.stop()
    wall, cpu = time.monotonic() - t0, time.process_time() - c0
    print(f"CPU: {100 * cpu / wall:.1f}% of one core over {wall:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Generate sounds/errors/<code>.wav with the server's BMO voice (run once, commit the WAVs).

Usage: venv/bin/python tools/make_error_clips.py [--force]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config  # noqa: E402
from app.notify import ERRORS, clip_path, spoken_text  # noqa: E402
from app.server.client import BMOClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="regenerate existing clips")
    args = ap.parse_args()
    cfg = load_config()
    client = BMOClient(cfg)
    sounds = cfg.path(cfg.sounds.dir)
    for code in ERRORS:
        path = clip_path(sounds, code)
        if path.exists() and not args.force:
            print(f"skip  {path.relative_to(sounds.parent)}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(client.speech(spoken_text(code)))
        print(f"wrote {path.relative_to(sounds.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

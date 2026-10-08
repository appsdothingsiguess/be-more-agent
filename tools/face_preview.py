#!/usr/bin/env python3
"""Render the SVG faces to a PNG contact sheet (needs PIL, no display).

  venv/bin/python tools/face_preview.py [out.png] [--morph a b]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from app.ui import face_svg  # noqa: E402

W, H = 400, 240


def render(face, w=W, h=H):
    img = Image.new("RGB", (w, h), tuple(int(c) for c in face.bg))
    d = ImageDraw.Draw(img)
    for kind, pts, color, width in face_svg.draw_ops(face, w / 800, h / 480):
        xy = list(zip(pts[0::2], pts[1::2]))
        if kind == "poly":
            d.polygon(xy, fill=color)
        else:
            d.line(xy, fill=color, width=max(1, round(width)), joint="curve")
            r = width / 2
            for x, y in (xy[0], xy[-1]):
                d.ellipse([x - r, y - r, x + r, y + r], fill=color)
    return img


def main():
    args = sys.argv[1:]
    out = Path(args[0]) if args and not args[0].startswith("--") else Path("faces_preview.png")
    faces = face_svg.load_faces(Path(__file__).resolve().parent.parent / "faces_svg")
    if "--morph" in args:
        a, b = args[args.index("--morph") + 1:][:2]
        names, frames = [], []
        for i in range(6):
            t = i / 5
            cur, tgt = faces[a].copy(), faces[b].copy()
            face_svg.pad_pair(cur, tgt)
            face_svg.approach(cur, tgt, t)
            frames.append(cur)
    else:
        frames = list(faces.values())
    cols = 4
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * W, rows * H), "white")
    for i, f in enumerate(frames):
        sheet.paste(render(f), ((i % cols) * W, (i // cols) * H))
    sheet.save(out)
    print(out, len(frames))


main()

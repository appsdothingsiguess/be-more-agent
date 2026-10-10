"""Restricted-SVG face loader and tweening, with no Tk or PIL dependency.

Each expression SVG (800x480, circle/ellipse/path with M L H V Q T C S Z, solid colours) is
turned into a Face: named parts (eye-left, mouth, tongue, ...) made of shapes, each shape a
fixed number of points. Equal point counts let any two faces be blended point by point, which
is how the Tk canvas animates between expressions.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

VIEW_W, VIEW_H = 800.0, 480.0
POINTS = 96
DEFAULT_BG = (194, 222, 172)

# Back to front. Unknown part ids are drawn first.
Z_ORDER = ("blush-left", "blush-right", "mouth", "tongue", "teeth",
           "eye-left", "eye-right", "brow-left", "brow-right")
MOUTH_PARTS = ("mouth", "tongue", "teeth")

RGBA = tuple  # (r, g, b, a) with r,g,b 0..255 and a 0..1


@dataclass
class Shape:
    pts: list            # [x0, y0, x1, y1, ...] flat, length 2 * POINTS
    closed: bool
    fill: RGBA
    stroke: RGBA
    width: float

    def copy(self) -> "Shape":
        return Shape(list(self.pts), self.closed, self.fill, self.stroke, self.width)

    def centroid(self) -> tuple[float, float]:
        n = len(self.pts) // 2
        return (sum(self.pts[0::2]) / n, sum(self.pts[1::2]) / n)

    def collapsed(self) -> "Shape":
        """An invisible, zero-size copy: what a part grows from or shrinks to."""
        cx, cy = self.centroid()
        return Shape([cx, cy] * (len(self.pts) // 2), self.closed,
                     self.fill[:3] + (0.0,), self.stroke[:3] + (0.0,), 0.0)


@dataclass
class Face:
    bg: tuple = DEFAULT_BG
    parts: dict = field(default_factory=dict)   # name -> list[Shape]

    def copy(self) -> "Face":
        return Face(self.bg, {k: [s.copy() for s in v] for k, v in self.parts.items()})


# ---------------------------------------------------------------- parsing

_TOKEN = re.compile(r"[MmLlHhVvQqTtCcSsZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_ARGS = {"M": 2, "L": 2, "H": 1, "V": 1, "Q": 4, "T": 2, "C": 6, "S": 4, "Z": 0}


def _quad(p0, p1, p2, steps=18):
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1])
            for t in (i / steps for i in range(1, steps + 1))]


def _cubic(p0, p1, p2, p3, steps=24):
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def parse_path(d: str) -> list:
    """Flatten an SVG path into [(points, closed), ...], one entry per subpath."""
    tokens = _TOKEN.findall(d)
    subpaths: list = []
    pts: list = []
    closed = False
    cur = start = (0.0, 0.0)
    last_ctrl = None       # previous quadratic/cubic control point, for T / S
    last_cmd = ""
    i, cmd = 0, ""

    def flush():
        nonlocal pts, closed
        if len(pts) >= 2:
            subpaths.append((pts, closed))
        pts, closed = [], False

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                if pts:
                    closed = True
                    flush()
                cur = start
                last_ctrl, last_cmd = None, "Z"
                continue
        elif not cmd:
            raise ValueError("path data must start with a command")
        up = cmd.upper()
        n = _ARGS[up]
        if up == "Z" or i + n > len(tokens):
            break
        a = [float(t) for t in tokens[i:i + n]]
        i += n
        rel = cmd.islower()
        ox, oy = cur if rel else (0.0, 0.0)

        if up == "M":
            flush()
            cur = start = (a[0] + ox, a[1] + oy)
            pts = [cur]
            cmd = "l" if rel else "L"        # extra pairs after M are implicit lineto
            last_ctrl = None
        elif up == "L":
            cur = (a[0] + ox, a[1] + oy)
            pts.append(cur)
            last_ctrl = None
        elif up == "H":
            cur = (a[0] + ox, cur[1])
            pts.append(cur)
            last_ctrl = None
        elif up == "V":
            cur = (cur[0], a[0] + oy)
            pts.append(cur)
            last_ctrl = None
        elif up == "Q":
            c, e = (a[0] + ox, a[1] + oy), (a[2] + ox, a[3] + oy)
            pts += _quad(cur, c, e)
            cur, last_ctrl = e, c
        elif up == "T":
            c = (2 * cur[0] - last_ctrl[0], 2 * cur[1] - last_ctrl[1]) \
                if last_ctrl and last_cmd in ("Q", "T") else cur
            e = (a[0] + ox, a[1] + oy)
            pts += _quad(cur, c, e)
            cur, last_ctrl = e, c
        elif up == "C":
            c1, c2, e = (a[0] + ox, a[1] + oy), (a[2] + ox, a[3] + oy), (a[4] + ox, a[5] + oy)
            pts += _cubic(cur, c1, c2, e)
            cur, last_ctrl = e, c2
        elif up == "S":
            c1 = (2 * cur[0] - last_ctrl[0], 2 * cur[1] - last_ctrl[1]) \
                if last_ctrl and last_cmd in ("C", "S") else cur
            c2, e = (a[0] + ox, a[1] + oy), (a[2] + ox, a[3] + oy)
            pts += _cubic(cur, c1, c2, e)
            cur, last_ctrl = e, c2
        last_cmd = up
    flush()
    return subpaths


def parse_color(value: str | None, default: tuple | None) -> RGBA:
    """'#rgb' / '#rrggbb' / a few names -> (r, g, b, 1); 'none' -> alpha 0."""
    if value is None:
        value = default
    if value is None or value == "none":
        return (0, 0, 0, 0.0)
    if isinstance(value, tuple):
        return value
    value = value.strip().lower()
    names = {"black": "#000000", "white": "#ffffff"}
    value = names.get(value, value)
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        value = "#" + "".join(c * 2 for c in value[1:])
    if not re.fullmatch(r"#[0-9a-f]{6}", value):
        raise ValueError(f"unsupported colour {value!r}")
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16), 1.0)


def _area(points) -> float:
    s = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
        s += x0 * y1 - x1 * y0
    return s / 2


def _resample(points, closed: bool, n: int = POINTS) -> list:
    """n points evenly spaced by arc length, as a flat [x, y, ...] list."""
    seq = points + points[:1] if closed else list(points)
    seg = [math.dist(a, b) for a, b in zip(seq, seq[1:])]
    total = sum(seg)
    if total < 1e-6:
        return [seq[0][0], seq[0][1]] * n
    count = n if closed else n - 1
    out, j, acc = [], 0, 0.0
    for k in range(n):
        target = total * k / count
        while j < len(seg) - 1 and acc + seg[j] < target:
            acc += seg[j]
            j += 1
        f = 0.0 if seg[j] == 0 else min(1.0, max(0.0, (target - acc) / seg[j]))
        (x0, y0), (x1, y1) = seq[j], seq[j + 1]
        out += [x0 + (x1 - x0) * f, y0 + (y1 - y0) * f]
    return out


def _canonical(flat: list, closed: bool) -> list:
    """Pick a start point and direction so similar shapes line up when blended.

    Open lines are stored as an out-and-back loop (left to right, then back), so they blend
    sensibly with closed shapes: eye circle <-> blink line, smile curve <-> open mouth.
    """
    pairs = list(zip(flat[0::2], flat[1::2]))
    if closed:
        if _area(pairs) < 0:                    # make every closed shape clockwise on screen
            pairs.reverse()
        k = min(range(len(pairs)), key=lambda i: (round(pairs[i][0], 3), pairs[i][1]))
        pairs = pairs[k:] + pairs[:k]
    else:
        if pairs[0] > pairs[-1]:
            pairs.reverse()                     # open lines run left to right
        pairs = pairs + pairs[-2:0:-1]
    return [c for p in pairs for c in p]


def _ellipse_points(cx, cy, rx, ry, n=POINTS) -> list:
    return [(cx - rx * math.cos(2 * math.pi * k / n), cy - ry * math.sin(2 * math.pi * k / n))
            for k in range(n)]


def _part_name(elem_id: str | None) -> str | None:
    if not elem_id:
        return None
    return "mouth" if elem_id.startswith("mouth") else elem_id


def _style(elem, key: str, default: str | None) -> str | None:
    if key in elem.attrib:
        return elem.attrib[key]
    m = re.search(rf"(?:^|;)\s*{key}\s*:\s*([^;]+)", elem.attrib.get("style", ""))
    return m.group(1).strip() if m else default


def parse_svg(text: str) -> Face:
    root = ET.fromstring(text)
    face = Face()
    vb = [float(v) for v in re.split(r"[ ,]+", root.attrib.get("viewBox", "0 0 800 480").strip())]
    sx, sy = VIEW_W / vb[2], VIEW_H / vb[3]

    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "rect":
            w, h = float(elem.attrib.get("width", 0)), float(elem.attrib.get("height", 0))
            if w >= vb[2] * 0.99 and h >= vb[3] * 0.99:
                c = parse_color(_style(elem, "fill", "#000000"), None)
                face.bg = c[:3]
            continue
        if tag not in ("circle", "ellipse", "path"):
            continue
        name = _part_name(elem.attrib.get("id"))
        if name is None:
            continue
        fill = parse_color(_style(elem, "fill", "#000000"), None)
        stroke = parse_color(_style(elem, "stroke", "none"), None)
        width = float(_style(elem, "stroke-width", "1") or 1) * sx if stroke[3] else 0.0

        raw: list = []                      # [(points, closed)]
        if tag == "circle":
            cx, cy, r = (float(elem.attrib[k]) for k in ("cx", "cy", "r"))
            raw.append((_ellipse_points(cx, cy, r, r), True))
        elif tag == "ellipse":
            cx, cy, rx, ry = (float(elem.attrib[k]) for k in ("cx", "cy", "rx", "ry"))
            raw.append((_ellipse_points(cx, cy, rx, ry), True))
        else:
            raw = parse_path(elem.attrib.get("d", ""))

        for points, closed in raw:
            points = [(x * sx, y * sy) for x, y in points]
            # A path is only filled if it encloses area: open strokes like "M L" stay lines.
            shape_fill = fill if abs(_area(points)) > 4 else fill[:3] + (0.0,)
            if closed:
                flat = _canonical(_resample(points, True), True)
            else:
                flat = _canonical(_resample(points, False, POINTS // 2 + 1), False)
            face.parts.setdefault(name, []).append(
                Shape(flat, True, shape_fill, stroke, width))
    return face


def load_faces(directory: Path) -> dict:
    """Map expression name -> Face for every NN_name.svg in directory ('01_neutral.svg')."""
    out: dict = {}
    for path in sorted(Path(directory).glob("*.svg")):
        name = re.sub(r"^\d+_", "", path.stem)
        try:
            out[name] = parse_svg(path.read_text(encoding="utf-8"))
        except (ET.ParseError, ValueError, KeyError) as e:
            raise ValueError(f"{path.name}: {e}") from e
    return out


# ---------------------------------------------------------------- tweening

def pad_pair(a: Face, b: Face) -> None:
    """Give a and b the same parts and shape counts (in place). Missing shapes are invisible."""
    for name in set(a.parts) | set(b.parts):
        la, lb = a.parts.setdefault(name, []), b.parts.setdefault(name, [])
        while len(la) < len(lb):
            la.append(lb[len(la)].collapsed())
        while len(lb) < len(la):
            lb.append(la[len(lb)].collapsed())


def _mix(x, y, t):
    return x + (y - x) * t


def _mix_color(a: RGBA, b: RGBA, t: float) -> RGBA:
    # An absent colour (alpha 0) has no hue of its own: borrow the other side's.
    if a[3] == 0:
        a = b[:3] + (0.0,)
    if b[3] == 0:
        b = a[:3] + (0.0,)
    # Fade in faster than out, so a swap (stroke -> fill) never dips to a ghost.
    ta = min(1.0, t * 2.5) if b[3] > a[3] else t
    return tuple(_mix(p, q, ta if i == 3 else t) for i, (p, q) in enumerate(zip(a, b)))


def approach(cur: Face, target: Face, t: float) -> float:
    """Move cur a fraction t toward target (both padded alike). Returns the largest change."""
    delta = 0.0
    cur.bg = tuple(_mix(p, q, t) for p, q in zip(cur.bg, target.bg))
    for name, shapes in cur.parts.items():
        for s, g in zip(shapes, target.parts[name]):
            cp, gp = s.pts, g.pts
            for k in range(len(cp)):
                d = gp[k] - cp[k]
                if d:
                    cp[k] += d * t
                    if abs(d) > delta:
                        delta = abs(d)
            delta = max(delta, abs(g.width - s.width), 20 * abs(g.fill[3] - s.fill[3]),
                        20 * abs(g.stroke[3] - s.stroke[3]))
            s.fill = _mix_color(s.fill, g.fill, t)
            s.stroke = _mix_color(s.stroke, g.stroke, t)
            s.width = _mix(s.width, g.width, t)
            s.closed = g.closed if t >= 0.5 else s.closed
    return delta


def snap(cur: Face, target: Face) -> None:
    approach(cur, target, 1.0)
    for name, shapes in cur.parts.items():
        for s, g in zip(shapes, target.parts[name]):
            s.closed = g.closed


# ---------------------------------------------------------------- drawing

def _hex(c, bg, alpha) -> str:
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, round(_mix(b, v, alpha)))) for v, b in zip(c[:3], bg))


def draw_ops(face: Face, scale_x: float, scale_y: float, offsets: dict | None = None) -> list:
    """Drawing list [(kind, flat_coords, colour_hex, width)] back to front.

    kind is 'poly' (filled) or 'line' (round-capped stroke). offsets shifts whole parts
    ({'eye-left': (dx, dy)}) in SVG units, used for eye drift.
    """
    offsets = offsets or {}
    names = sorted(face.parts, key=lambda n: Z_ORDER.index(n) if n in Z_ORDER else -1)
    ops = []
    for name in names:
        dx, dy = offsets.get(name, (0.0, 0.0))
        for s in face.parts[name]:
            coords = [(c + dx) * scale_x if i % 2 == 0 else (c + dy) * scale_y
                      for i, c in enumerate(s.pts)]
            if s.fill[3] > 0.03:
                ops.append(("poly", coords, _hex(s.fill, face.bg, s.fill[3]), 0.0))
            if s.stroke[3] > 0.03 and s.width * scale_x > 0.4:
                line = coords + coords[:2] if s.closed else coords
                ops.append(("line", line, _hex(s.stroke, face.bg, s.stroke[3]),
                            s.width * scale_x))
    return ops


# ---------------------------------------------------------------- generated eyes

def _generated(points: list, fill: RGBA) -> Shape:
    return Shape(_canonical(_resample(points, True), True), True, fill, fill[:3] + (0.0,), 0.0)


def heart_shape(cx: float, cy: float, size: float, fill: RGBA = (226, 44, 84, 1.0)) -> Shape:
    """A heart about 2*size wide, centred on (cx, cy): for heart eyes."""
    k = size / 16.0
    pts = []
    for i in range(180):
        t = 2 * math.pi * i / 180
        pts.append((cx + k * 16 * math.sin(t) ** 3,
                    cy - k * (13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t)
                              - math.cos(4 * t)) - k * 2))
    return _generated(pts, fill)


def star_shape(cx: float, cy: float, size: float, fill: RGBA = (0, 0, 0, 1.0)) -> Shape:
    """A four-point sparkle about 2*size across, centred on (cx, cy): for sparkle eyes."""
    pts = []
    for i in range(8):
        a = math.pi / 2 * (i // 2) + (math.pi / 4 if i % 2 else 0.0) - math.pi / 2
        r = size if i % 2 == 0 else size * 0.3
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return _generated(pts, fill)

"""Sizing a shape to its words and checking a drawing's layout from geometry alone (DESIGN.md §56.5).

Pure: no browser. One text measure serves the shape actions, ``read``'s warnings and Mermaid
conversion (``board_flow`` takes ``fit`` from here), so a label the diagram converter sized to fit never
draws an overflow warning. The pane measures real text; these are estimates. ``warnings`` is what
``read`` appends and every write returns for what it touched, so an agent can fix a drawing before it
announces it.
"""

from __future__ import annotations

import math
import textwrap
from dataclasses import dataclass
from typing import Any

GLYPH = 8.6  # pixels one character of the board's 16 px font takes
LINE = 20.0  # one line of that font
BASE_FONT = 16.0
MAX_WARNINGS = 20
_SHAPES = ("rectangle", "ellipse", "diamond")
KINDS = {"rectangle": "rect", "ellipse": "circle", "diamond": "diamond"}
_WRAP = {"rect": 25, "round": 25, "stadium": 25, "circle": 20, "diamond": 16}  # characters a line may take
_EPS = 1e-6


def wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for part in text.split("\n"):
        lines.extend(
            textwrap.wrap(part, max(1, width), break_long_words=True, break_on_hyphens=False) or [""]
        )
    return lines


@dataclass(frozen=True)
class Fit:
    lines: list[str]
    text_w: float
    text_h: float
    w: float
    h: float


def _grow(kind: str, tw: float, th: float) -> tuple[float, float]:
    """The box a block of text ``tw`` by ``th`` needs: the shape's own padding and slack."""
    if kind == "diamond":
        return max(140.0, 2 * tw + 30), max(90.0, 2 * th + 40)
    if kind == "circle":
        return max(140.0, 1.45 * tw + 40), max(80.0, 1.45 * th + 40)
    return max(140.0, tw + 40), max(80.0, th + 40)


def fit(label: str, kind: str, font_size: float = BASE_FONT) -> Fit:
    """A box for the label: wrapped, and tall enough for every line."""
    glyph, line = GLYPH * font_size / BASE_FONT, LINE * font_size / BASE_FONT
    lines = wrap(label, _WRAP.get(kind, 25))
    tw, th = glyph * max(len(x) for x in lines), line * len(lines)
    w, h = _grow(kind, tw, th)
    return Fit(lines, tw, th, w, h)


def _inner(kind: str, w: float, h: float) -> tuple[float, float]:
    """The room a label has inside a shape of this size (the inverse of ``_grow``)."""
    if kind == "diamond":
        return max(1.0, (w - 30) / 2), max(1.0, (h - 40) / 2)
    if kind == "circle":
        return max(1.0, (w - 40) / 1.45), max(1.0, (h - 40) / 1.45)
    return max(1.0, w - 40), max(1.0, h - 40)


def label_lines(kind: str, w: float, text: str, font_size: float = BASE_FONT) -> list[str]:
    """The lines a label takes in a shape ``w`` wide."""
    glyph = GLYPH * font_size / BASE_FONT
    return wrap(text, int(_inner(kind, w, 0)[0] / glyph + _EPS))


def height_for(kind: str, w: float, text: str, font_size: float = BASE_FONT, minimum: bool = True) -> float:
    """The height a shape ``w`` wide needs for its label (rounded up to 10; at least the default
    height unless ``minimum`` is False)."""
    if not text:
        return _grow(kind, 0, 0)[1] if minimum else 0.0
    th = LINE * font_size / BASE_FONT * len(label_lines(kind, w, text, font_size))
    h = _grow(kind, 0, th)[1]
    return float(math.ceil(h / 10) * 10) if minimum else h


def overflows(kind: str, w: float, h: float, text: str, font_size: float = BASE_FONT) -> bool:
    """Whether the label's lines are taller than the room inside the shape."""
    if not text:
        return False
    th = LINE * font_size / BASE_FONT * len(label_lines(kind, w, text, font_size))
    return th > _inner(kind, w, h)[1] + _EPS


def label_box(shape: dict[str, Any], text: str, font_size: float) -> tuple[float, float]:
    """The label's width and height inside ``shape``."""
    kind = KINDS.get(shape["type"], "rect")
    width = max(20.0, _inner(kind, shape["width"], shape["height"])[0])
    th = max(25.0, LINE * font_size / BASE_FONT * len(label_lines(kind, shape["width"], text, font_size)))
    return width, th


def loose_size(text: str, font_size: float) -> tuple[float, float]:
    """The box of a text with no shape around it: one line up to 520 wide, then wrapped."""
    glyph, line = GLYPH * font_size / BASE_FONT, LINE * font_size / BASE_FONT
    w = max(40.0, min(520.0, len(text) * glyph + 10))
    return w, max(25.0, line * len(wrap(text, int((w - 10) / glyph + _EPS))))


def _live(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in elements if not e.get("isDeleted")]


def _text(t: dict[str, Any]) -> str:
    return str(t.get("originalText") or t.get("text") or "")


def _hit(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _box(e: dict[str, Any]) -> tuple[float, float, float, float]:
    return e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"]


def _segment_hits(
    p: tuple[float, float], q: tuple[float, float], r: tuple[float, float, float, float]
) -> bool:
    """Liang-Barsky: does the segment p-q cross the rectangle r?"""
    x0, y0 = p
    dx, dy = q[0] - p[0], q[1] - p[1]
    lo, hi = 0.0, 1.0
    for d, a in ((-dx, x0 - r[0]), (dx, r[2] - x0), (-dy, y0 - r[1]), (dy, r[3] - y0)):
        if d == 0:
            if a < 0:
                return False
        else:
            t = a / d
            if d < 0:
                lo = max(lo, t)
            else:
                hi = min(hi, t)
            if lo > hi:
                return False
    return True


def route(a: dict[str, Any], src: dict[str, Any], dst: dict[str, Any]) -> list[tuple[float, float]]:
    """Where an arrow runs. Its stored points when it has a route of its own (an elbow, or several
    points, as ``connect``, ``arrange`` and Mermaid conversion store them); a plain two-point arrow is
    drawn by the pane from its bindings, centre to centre."""
    raw = a.get("points")
    pts = [
        (a["x"] + pt[0], a["y"] + pt[1])
        for pt in (raw if isinstance(raw, list) else [])
        if isinstance(pt, (list, tuple)) and len(pt) == 2
    ]
    if len(pts) > 2 or (len(pts) == 2 and a.get("elbowed")):
        return pts
    p = (src["x"] + src["width"] / 2, src["y"] + src["height"] / 2)
    q = (dst["x"] + dst["width"] / 2, dst["y"] + dst["height"] / 2)
    return [p, q]


def _midpoint(poly: list[tuple[float, float]]) -> tuple[float, float]:
    """The point half-way along the polyline's length."""
    lengths = [math.dist(a, b) for a, b in zip(poly, poly[1:], strict=False)]
    half = sum(lengths) / 2
    for (a, b), n in zip(zip(poly, poly[1:], strict=False), lengths, strict=True):
        if n and half <= n:
            t = half / n
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        half -= n
    return poly[0]


def warnings(elements: list[dict[str, Any]], only: set[str] | None = None) -> list[str]:
    """Layout problems found from geometry. Empty means nothing to fix.

    ``only`` keeps the problems that name one of those ids (a write's result is about what it touched),
    and checks only the pairs that involve one, so a write on a full board stays cheap.
    """
    live = _live(elements)
    by_id = {e["id"]: e for e in live}
    labels_of: dict[str, dict[str, Any]] = {}
    for e in live:
        if e["type"] == "text" and e.get("containerId"):
            labels_of.setdefault(e["containerId"], e)
    shapes = [e for e in live if e["type"] in _SHAPES]
    arrows = [e for e in live if e["type"] == "arrow"]

    def hot(*ids: str) -> bool:
        return only is None or any(i in only for i in ids)

    found: list[tuple[tuple[str, ...], str]] = []
    touched_shapes = [s for s in shapes if hot(s["id"])]
    seen: set[tuple[str, str]] = set()
    for a in touched_shapes:
        for b in shapes:
            key = (a["id"], b["id"]) if a["id"] < b["id"] else (b["id"], a["id"])
            if a is b or key in seen:
                continue
            seen.add(key)
            if _hit(_box(a), _box(b)):
                found.append((key, f"overlap: {key[0]} and {key[1]}"))
    for s in touched_shapes:
        label = labels_of.get(s["id"])
        if label is not None:
            fs = float(label.get("fontSize") or BASE_FONT)
            if overflows(KINDS[s["type"]], s["width"], s["height"], _text(label), fs):
                found.append(
                    (
                        (s["id"],),
                        f"label may overflow: {s['id']} (make it shorter, or resize wider or taller)",
                    )
                )
        frame = by_id.get(s.get("frameId") or "")
        if frame is not None and frame["type"] == "frame":
            sb, fb = _box(s), _box(frame)
            inside = sb[0] >= fb[0] and sb[1] >= fb[1] and sb[2] <= fb[2] and sb[3] <= fb[3]
            if not inside:
                kind = "straddles" if _hit(sb, fb) else "is outside"
                found.append(((s["id"], frame["id"]), f"{s['id']} {kind} its frame {frame['id']}"))
    labels: list[tuple[str, tuple[float, float, float, float]]] = []
    for a in arrows:
        ends = [(a.get(k) or {}).get("elementId") for k in ("startBinding", "endBinding")]
        src, dst = by_id.get(ends[0] or ""), by_id.get(ends[1] or "")
        if src is None or dst is None:
            continue
        poly = route(a, src, dst)
        for s in shapes if hot(a["id"]) else touched_shapes:
            if s["id"] in (src["id"], dst["id"]):
                continue
            x0, y0, x1, y1 = _box(s)
            r = (x0 + 2, y0 + 2, x1 - 2, y1 - 2)
            if any(_segment_hits(p, q, r) for p, q in zip(poly, poly[1:], strict=False)):
                found.append(((a["id"], s["id"]), f"arrow {a['id']} passes through {s['id']}"))
        label = labels_of.get(a["id"])
        if label is not None:
            fs = float(label.get("fontSize") or 16)
            w = min(len(_text(label)), 40) * GLYPH * fs / BASE_FONT
            mx, my = _midpoint(poly)
            labels.append((a["id"], (mx - w / 2, my - fs, mx + w / 2, my + fs)))
    pairs: set[tuple[str, str]] = set()
    for ia, ba in (item for item in labels if hot(item[0])):
        for ib, bb in labels:
            key = (ia, ib) if ia < ib else (ib, ia)
            if ia == ib or key in pairs:
                continue
            pairs.add(key)
            if _hit(ba, bb):
                found.append((key, f"arrow labels overlap: {key[0]} and {key[1]}"))
    out = [text for ids, text in found if only is None or only.intersection(ids)]
    if len(out) > MAX_WARNINGS:
        out = [*out[:MAX_WARNINGS], f"... and {len(out) - MAX_WARNINGS} more"]
    return out

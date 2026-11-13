"""Sizing text and checking a drawing's layout from geometry alone (DESIGN.md §56.5).

Pure: no browser. The pane measures real text, so these are estimates, kept on the cautious side:
a shape grows a little more than it needs rather than a label spilling out of it. ``warnings`` is
what ``read`` appends so an agent can fix a drawing before it announces it.
"""

from __future__ import annotations

import math
from typing import Any

CHAR = 0.6  # a character's width, in font sizes (the hand-drawn font runs narrower; this errs wide)
LINE = 1.25  # line height, in font sizes
MAX_WARNINGS = 20
_SHAPES = ("rectangle", "ellipse", "diamond")
# How much of a shape's width a bound label may use, and the padding it needs around the text.
_INNER = {"rectangle": (1.0, 20.0), "ellipse": (0.7, 10.0), "diamond": (0.5, 10.0)}


def inner_width(kind: str, w: float) -> float:
    k, pad = _INNER.get(kind, _INNER["rectangle"])
    return max(10.0, w * k - pad)


def wrap(text: str, font_size: float, width: float) -> int:
    """How many lines ``text`` takes in ``width``: greedy by word, a long word split by letter."""
    per = max(1, int(width // (CHAR * font_size)))
    lines = 0
    for para in text.split("\n") or [""]:
        cur = 0
        lines += 1
        for word in para.split():
            n = len(word)
            if cur and cur + 1 + n <= per:
                cur += 1 + n
            elif not cur and n <= per:
                cur = n
            else:
                if cur:
                    lines += 1
                lines += (n - 1) // per
                cur = n - ((n - 1) // per) * per
    return lines


def text_height(text: str, font_size: float, width: float) -> float:
    return wrap(text, font_size, width) * font_size * LINE


def fit_height(kind: str, w: float, text: str, font_size: float, minimum: float = 80.0) -> float:
    """The height a shape of width ``w`` needs for its label, never under ``minimum``."""
    if not text:
        return minimum
    th = text_height(text, font_size, inner_width(kind, w))
    need = {"rectangle": th + 32, "ellipse": th * 1.5 + 24, "diamond": th * 2.2 + 24}.get(kind, th + 32)
    return float(max(minimum, math.ceil(need / 10) * 10))


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


def elbow_route(p: tuple[float, float], q: tuple[float, float]) -> list[tuple[float, float]]:
    """The right-angled route the pane draws for an elbow arrow stored with only its two end points:
    along the longer axis to the middle, across, then along again."""
    if abs(q[0] - p[0]) >= abs(q[1] - p[1]):
        mx = (p[0] + q[0]) / 2
        return [p, (mx, p[1]), (mx, q[1]), q]
    my = (p[1] + q[1]) / 2
    return [p, (p[0], my), (q[0], my), q]


def route(a: dict[str, Any], src: dict[str, Any], dst: dict[str, Any]) -> list[tuple[float, float]]:
    """Where an arrow runs. Its stored points when it has a route of its own (an elbow, or several
    points); a plain two-point arrow is drawn by the pane from its bindings, centre to centre."""
    raw = a.get("points")
    pts = [
        (a["x"] + pt[0], a["y"] + pt[1])
        for pt in (raw if isinstance(raw, list) else [])
        if isinstance(pt, (list, tuple)) and len(pt) == 2
    ]
    if len(pts) == 2 and a.get("elbowed"):
        return elbow_route(pts[0], pts[1])
    if len(pts) > 2:
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
            fs = float(label.get("fontSize") or 16)
            inner = inner_width(s["type"], s["width"])
            words = max((len(w) for w in _text(label).split()), default=0)
            if (
                fit_height(s["type"], s["width"], _text(label), fs, 0) > s["height"]
                or words * CHAR * fs > inner
            ):
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
            w = min(len(_text(label)), 40) * CHAR * fs
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

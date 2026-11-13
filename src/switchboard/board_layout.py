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


def _label_of(elements: list[dict[str, Any]], el: dict[str, Any]) -> dict[str, Any] | None:
    for b in el.get("boundElements") or []:
        if isinstance(b, dict) and b.get("type") == "text":
            t = next((e for e in elements if e["id"] == b.get("id")), None)
            if t and t["type"] == "text" and not t.get("isDeleted"):
                return t
    return None


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


def warnings(elements: list[dict[str, Any]], only: set[str] | None = None) -> list[str]:
    """Layout problems found from geometry. Empty means nothing to fix.

    ``only`` keeps the problems that name one of those ids (a write's result is about what it touched).
    """
    live = _live(elements)
    by_id = {e["id"]: e for e in live}
    shapes = [e for e in live if e["type"] in _SHAPES]
    found: list[tuple[tuple[str, ...], str]] = []
    for i, a in enumerate(shapes):
        for b in shapes[i + 1 :]:
            if _hit(_box(a), _box(b)):
                found.append(((a["id"], b["id"]), f"overlap: {a['id']} and {b['id']}"))
    for s in shapes:
        label = _label_of(live, s)
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
    for a in live:
        if a["type"] != "arrow":
            continue
        ends = [(a.get(k) or {}).get("elementId") for k in ("startBinding", "endBinding")]
        src, dst = by_id.get(ends[0] or ""), by_id.get(ends[1] or "")
        if src is None or dst is None:
            continue
        p = (src["x"] + src["width"] / 2, src["y"] + src["height"] / 2)
        q = (dst["x"] + dst["width"] / 2, dst["y"] + dst["height"] / 2)
        for s in shapes:
            if s["id"] in (src["id"], dst["id"]):
                continue
            x0, y0, x1, y1 = _box(s)
            if _segment_hits(p, q, (x0 + 2, y0 + 2, x1 - 2, y1 - 2)):
                found.append(((a["id"], s["id"]), f"arrow {a['id']} passes through {s['id']}"))
        label = _label_of(live, a)
        if label is not None:
            fs = float(label.get("fontSize") or 16)
            w = min(len(_text(label)), 40) * CHAR * fs
            mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
            labels.append((a["id"], (mx - w / 2, my - fs, mx + w / 2, my + fs)))
    for i, (ia, ba) in enumerate(labels):
        for ib, bb in labels[i + 1 :]:
            if _hit(ba, bb):
                found.append(((ia, ib), f"arrow labels overlap: {ia} and {ib}"))
    out = [text for ids, text in found if only is None or only.intersection(ids)]
    if len(out) > MAX_WARNINGS:
        out = [*out[:MAX_WARNINGS], f"... and {len(out) - MAX_WARNINGS} more"]
    return out

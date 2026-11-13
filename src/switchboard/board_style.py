"""What an agent may set on a shape: Excalidraw's palette and the ``style`` string (DESIGN.md §56.3).

Pure, and imports nothing from the board, so the Mermaid side can use it too. A style is
space-separated ``key=value`` pairs in Excalidraw's own words (the style panel's), palette names only
and no raw hex. Anything unknown fails closed with the accepted list. ``fields`` maps a parsed style
to element fields for one kind of target; the board merge and the cleaner still check what is stored.
"""

from __future__ import annotations

import re
from typing import Any

MAX_STYLE = 300
MAX_PAIRS = 12
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class StyleError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# Excalidraw's own colour palette, so an agent's colours match the picker a person uses. Taken from
# the pinned build (``web/static/vendor/board/chunks/chunk-42LTX363.js``, its Open Color table
# ``Jt``): the background picker's shade is index 2 of a hue, the stroke picker's index 8.
# ``black`` is the default stroke the picker shows (``#1e1e1e``), not the table's ``#000000``.
# ``indigo`` and ``lime`` are in the build but not in the picker's main list, so they are not names.
# name -> (fill, stroke)
PALETTE: dict[str, tuple[str, str]] = {
    "gray": ("#e9ecef", "#343a40"),
    "red": ("#ffc9c9", "#e03131"),
    "pink": ("#fcc2d7", "#c2255c"),
    "grape": ("#eebefa", "#9c36b5"),
    "violet": ("#d0bfff", "#6741d9"),
    "blue": ("#a5d8ff", "#1971c2"),
    "cyan": ("#99e9f2", "#0c8599"),
    "teal": ("#96f2d7", "#099268"),
    "green": ("#b2f2bb", "#2f9e44"),
    "yellow": ("#ffec99", "#f08c00"),
    "orange": ("#ffd8a8", "#e8590c"),
}
COLOR_NAMES = (*PALETTE, "black", "white", "transparent")
DEFAULT_STROKE = "#1e1e1e"
_NEUTRAL = {"black": DEFAULT_STROKE, "white": "#ffffff", "transparent": "transparent"}


def color_hex(name: Any, role: str) -> str:
    """The hex for a palette name as a ``fill`` (the background picker's shade) or a ``stroke``.

    Names only, in lower case: raw hex, any other word and a non-string fail closed with the list.
    """
    if isinstance(name, str):
        if name in PALETTE:
            return PALETTE[name][0 if role == "fill" else 1]
        if name in _NEUTRAL:
            return _NEUTRAL[name]
    raise StyleError(f"unknown colour; use one of: {', '.join(COLOR_NAMES)}")


def _rgb(h: str) -> tuple[int, int, int] | None:
    if not isinstance(h, str) or not _HEX.fullmatch(h):
        return None
    d = h[1:]
    if len(d) in (3, 4):
        d = "".join(c * 2 for c in d)
    return int(d[0:2], 16), int(d[2:4], 16), int(d[4:6], 16)


def nearest_name(value: Any, role: str) -> str:
    """The palette name closest to a stored colour, for what a person picked by hand.

    ``transparent`` and an unreadable value stay ``transparent``; the rest is the nearest of the
    palette's colours for that role in RGB, ties to the earlier name.
    """
    rgb = _rgb(value)
    if rgb is None:
        return "transparent"
    best, gap = "transparent", None
    for name in (*PALETTE, "black", "white"):
        cand = _rgb(color_hex(name, role))
        assert cand is not None
        d = sum((a - b) ** 2 for a, b in zip(rgb, cand, strict=True))
        if gap is None or d < gap:
            best, gap = name, d
    return best


# value tables: Excalidraw's style panel, in its own words
_WIDTH = {"thin": 1, "bold": 2, "extra-bold": 4}
_SLOPPY = {"architect": 0, "artist": 1, "cartoonist": 2}
_FONT = {"s": 16, "m": 20, "l": 28, "xl": 36}
_FILLS = ("hachure", "cross-hatch", "solid")
_LINES = ("solid", "dashed", "dotted")
_HEADS = ("none", "arrow", "bar", "dot", "triangle", "diamond")
_ARROWS = ("sharp", "curved", "elbow")
_ALIGNS = ("left", "center", "right")
_EDGES = ("sharp", "round")

_SHAPE_KEYS = (
    "stroke",
    "background",
    "fill",
    "stroke-width",
    "stroke-style",
    "sloppiness",
    "edges",
    "opacity",
)
_TEXT_KEYS = ("font-size", "align")
# A frame is drawn with Excalidraw's fixed frame style whatever it stores, so it takes no style.
_ALLOWED: dict[str, tuple[str, ...]] = {
    "shape": (*_SHAPE_KEYS, *_TEXT_KEYS),
    "text": ("stroke", "opacity", *_TEXT_KEYS),
    "arrow": (
        "stroke",
        "stroke-width",
        "stroke-style",
        "sloppiness",
        "opacity",
        "arrow-type",
        "start",
        "end",
        *_TEXT_KEYS,
    ),
    "frame": (),
}
_CHOICES: dict[str, tuple[str, ...]] = {
    "fill": _FILLS,
    "stroke-width": tuple(_WIDTH),
    "stroke-style": _LINES,
    "sloppiness": tuple(_SLOPPY),
    "edges": _EDGES,
    "font-size": tuple(_FONT),
    "align": _ALIGNS,
    "arrow-type": _ARROWS,
    "start": _HEADS,
    "end": _HEADS,
}


def accepted(target: str = "shape") -> str:
    keys = _ALLOWED[target]
    if not keys:
        return "a frame takes no style (it is drawn in Excalidraw's frame style); use w and h"
    parts = []
    for k in keys:
        if k in ("stroke", "background"):
            parts.append(f"{k}=<{'|'.join(COLOR_NAMES)}>")
        elif k == "opacity":
            parts.append("opacity=10..100")
        else:
            parts.append(f"{k}={'|'.join(_CHOICES[k])}")
    return "; ".join(parts)


def parse_style(value: Any) -> dict[str, str]:
    """``"stroke=red fill=solid"`` to a dict. Keys and values are checked later, per target."""
    if not isinstance(value, str):
        raise StyleError("style must be text like: stroke=red background=yellow")
    if len(value) > MAX_STYLE:
        raise StyleError(f"style is longer than {MAX_STYLE} characters")
    pairs = value.split()
    if len(pairs) > MAX_PAIRS:
        raise StyleError(f"style has more than {MAX_PAIRS} settings")
    out: dict[str, str] = {}
    for pair in pairs:
        key, eq, val = pair.partition("=")
        if not eq or not key or not val or key in out:
            raise StyleError(f"bad style setting {pair[:40]!r}: write key=value, once each")
        out[key] = val
    return out


def fields(style: dict[str, str], target: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Element fields for a parsed style: ``(on the shape or arrow, on its label text)``.

    ``target`` is ``shape``, ``text``, ``arrow`` or ``frame``. Raises ``StyleError`` with the accepted
    list for a key this target doesn't take or a value Excalidraw's panel doesn't offer.
    """
    allowed = _ALLOWED[target]
    el: dict[str, Any] = {}
    label: dict[str, Any] = {}
    for key, val in style.items():
        if key not in allowed:
            raise StyleError(f"{key!r} is not a style for this; use {accepted(target)}")
        if key == "stroke":
            put = label if target == "text" else el
            put["strokeColor"] = color_hex(val, "stroke")
        elif key == "background":
            el["backgroundColor"] = color_hex(val, "fill")
        elif key == "opacity":
            if not (re.fullmatch(r"[0-9]{1,3}", val) and 10 <= int(val) <= 100):
                raise StyleError("opacity must be a whole number from 10 to 100")
            (label if target == "text" else el)["opacity"] = int(val)
        else:
            if val not in _CHOICES[key]:
                raise StyleError(f"{key}={val[:30]!r} is not one of: {', '.join(_CHOICES[key])}")
            if key == "fill":
                el["fillStyle"] = val
            elif key == "stroke-width":
                el["strokeWidth"] = _WIDTH[val]
            elif key == "stroke-style":
                el["strokeStyle"] = val
            elif key == "sloppiness":
                el["roughness"] = _SLOPPY[val]
            elif key == "edges":
                el["roundness"] = {"type": 3} if val == "round" else None
            elif key == "font-size":
                label["fontSize"] = _FONT[val]
            elif key == "align":
                label["textAlign"] = val
            elif key == "arrow-type":
                el["elbowed"] = val == "elbow"
                el["roundness"] = {"type": 2} if val == "curved" else None
            else:  # start, end
                el["startArrowhead" if key == "start" else "endArrowhead"] = None if val == "none" else val
    return el, label

"""Seeing the call's shared screen (DESIGN.md §52).

joinly's ``get_video_snapshot`` hands back one image of the bot's view of the call. What
comes back is untrusted bytes from another program, so the type is decided by the bytes,
never by the type joinly claims. Pure: the bridge does the asking and the storing.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable
from typing import Any, NamedTuple

from switchboard.meeting.names import match

SNAPSHOT_GAP_S = 10.0  # at most one snapshot this often in a call

_SCREEN = re.compile(r"\bscreen\b", re.IGNORECASE)


def image_kind(data: bytes) -> str | None:
    """``png``, ``jpg`` or ``webp`` by the first bytes of ``data``, else None.

    These are the three a snapshot may be. A claimed type is not looked at: a body that
    starts with anything else is not an image, whatever it was called.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


class Asker(NamedTuple):
    """The person who asked for a snapshot: how the room names them, their id on a hosted broker
    (None for the owner), and how they asked (``web``)."""

    name: str
    person_id: int | None
    via: str


class NoFrame(Exception):
    """joinly gave no usable picture. The message is a sentence for the room."""


def frame_from(result: Any, limit: int) -> tuple[bytes, str]:
    """The picture in a ``get_video_snapshot`` result, and its kind (``png``, ``jpg``, ``webp``).

    The size is checked on the base64 text first, so a reply past ``limit`` bytes is never decoded.
    The kind is read from the bytes: the ``mimeType`` joinly sends is not looked at."""
    for block in getattr(result, "content", None) or []:
        raw = getattr(block, "data", None)
        if getattr(block, "type", "") != "image" or not isinstance(raw, str):
            continue
        if len(raw) > limit * 4 // 3 + 8:
            raise NoFrame(f"The snapshot is larger than the room's file limit ({limit / 2**20:g} MB).")
        try:
            data = base64.b64decode(raw, validate=True)
        except ValueError:
            raise NoFrame("joinly sent a picture that could not be read.") from None
        kind = image_kind(data)
        if kind is None:
            raise NoFrame("joinly sent something that is not a PNG, JPEG or WebP picture.")
        return data, kind
    raise NoFrame("joinly sent no picture.")


def says_screen(text: str) -> bool:
    """Whether ``text`` has the whole word "screen". "Screenshot" and "screening" do not count."""
    return _SCREEN.search(text) is not None


def wants_snapshot(text: str, members: Iterable[str]) -> bool:
    """A call line that addresses one agent, by `names.match`, and says "screen".

    "Claude, look at my screen" is one. "Look at my screen" addresses nobody, and a bare
    "Claude" with two Claudes in the room is ambiguous: neither takes a snapshot.
    """
    return says_screen(text) and match(text, members).outcome == "one"

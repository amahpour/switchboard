"""Showing a room's picture in the call (DESIGN.md §52.9).

A person's ``/meeting share <file>`` hands one picture of the room to the bot, which shows it as a screen
share. What goes to joinly is the picture's bytes, never a link, and only a PNG or a JPEG: the type is read
from the bytes, not from the file's name or the type it was stored with. Pure: the bridge does the asking.
"""

from __future__ import annotations

import base64
import re

from switchboard.files import FILE_ID_RE, FILE_REF_RE

# the most the joinly patch takes (deploy/joinly/server.py); a room's own file limit may be lower
SHARE_MAX_BYTES = 8 * 1024 * 1024
SHARE_NAME_MAX = 200

# what joinly's Zoom page says when the host has not let participants share (the tool's own sentence, and
# the dialog's words)
_HOST_REFUSED = re.compile(r"host disabled|disabled share|screen sharing is disabled", re.IGNORECASE)

NOT_ALLOWED = (
    "Zoom did not let the bot share: the host has not allowed participants to share. In Zoom, open Security"
    " and turn on Share Screen for all participants, then share again."
)
NOT_A_PICTURE = "Only a PNG or JPEG picture can be shared in a call."
BUSY = "The bot is already sharing a picture. /meeting share stop ends it first."
NOT_SHARING = "The bot is not sharing a picture."


class ShareError(Exception):
    """A picture that cannot be shared. The message is a sentence for the room."""


def picture_kind(data: bytes) -> str | None:
    """``png`` or ``jpg`` by the first bytes of ``data``, else None. A WebP, a GIF, an SVG or anything
    else is not a picture the bot shares, whatever its name says."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return None


def check_picture(data: bytes, limit: int) -> str:
    """The kind of ``data`` (``png`` or ``jpg``), or ShareError. ``limit`` is the room's own file cap; the
    patch's cap applies as well."""
    cap = min(limit, SHARE_MAX_BYTES)
    if len(data) > cap:
        raise ShareError(f"The picture is larger than the {cap / 2**20:g} MB a call share may be.")
    kind = picture_kind(data)
    if kind is None:
        raise ShareError(NOT_A_PICTURE)
    return kind


def encode(data: bytes) -> str:
    """The base64 text joinly's ``share_image`` takes. The only form a picture travels in."""
    return base64.b64encode(data).decode("ascii")


def reference(words: tuple[str, ...]) -> str:
    """The file a person named, as one string: ``switchboard:file/<id>``, an id, or a file's name (which may
    have had spaces). Raises ShareError when there is none, or it is too long."""
    ref = " ".join(words).strip()
    if not ref:
        raise ShareError("usage: /meeting share <file> (a picture in this room), or /meeting share stop")
    if len(ref) > SHARE_NAME_MAX:
        raise ShareError("That file name is too long.")
    return ref


def file_id_of(ref: str) -> str | None:
    """The stored file's id when ``ref`` is one (a bare id, or ``switchboard:file/<id>``), else None."""
    low = ref.lower()
    if FILE_ID_RE.fullmatch(low):
        return low
    m = FILE_REF_RE.fullmatch(low)
    return m.group(1) if m else None


def host_refused(text: str) -> bool:
    """Whether joinly's failure says the host has not allowed participants to share."""
    return _HOST_REFUSED.search(text) is not None

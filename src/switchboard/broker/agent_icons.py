"""The avatars an admin uploads for each harness (DESIGN.md §29.10, #498): what a file must be before the
broker stores it and sends it to every browser, and the stylesheet that shows it.

An upload is a PNG or a WebP, at most 256 KB and 512 pixels a side, and never an SVG (which can carry
script). The check reads the bytes and trusts nothing the sender says about them:

- the type is the file's own magic number, and a label that disagrees (a PNG sent as ``image/webp``) is
  refused, with a message that names both;
- the size is parsed from the header, for the three WebP layouts (lossless, lossy, extended) and a PNG
  (whose every chunk is walked, its CRC checked and the end of the file required to be the end of the
  data), so a file that is cut short, damaged, or a picture with something appended is refused;
- nothing is decoded and nothing is re-encoded: the server image has no imaging library, and the
  caps on bytes and pixels are what bound the work the browser does.

``stylesheet`` writes the page's CSS for the icons that are set. Every value in it is one of four harness
names, one of two schemes or twelve hex digits, so no byte of an upload or of a request reaches it.
"""

from __future__ import annotations

import hashlib
import re
import zlib
from dataclasses import dataclass

from switchboard.models import (
    ICON_HARNESSES,
    ICON_MAX_BYTES,
    ICON_MAX_SIDE,
    ICON_SCHEMES,
    ICON_TYPES,
    AgentIcon,
)

HARNESSES = ICON_HARNESSES
SCHEMES = ICON_SCHEMES
TYPES = ICON_TYPES
MAX_BYTES = ICON_MAX_BYTES
MAX_SIDE = ICON_MAX_SIDE

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_STATUS = {"too_large": 413, "svg": 415, "type": 415, "not_an_image": 415}
_VERSION_RE = re.compile(r"[0-9a-f]{12}")
# the colour types a PNG header may name, with the bit depths each allows
_PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}


class IconError(Exception):
    """A file that is not an avatar the broker will store: ``code`` for the page, ``message`` in plain
    words for the person, ``status`` for the HTTP answer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = _STATUS.get(code, 400)


@dataclass(frozen=True)
class Checked:
    """A file that passed: its type, its parsed size and its hash (the cache key is the hash's start)."""

    mime: str
    width: int
    height: int
    sha256: str

    @property
    def version(self) -> str:
        return self.sha256[:12]


def too_large(size: int | None = None) -> IconError:
    """The file is over the byte cap; ``size`` is None when the body was cut off while it was read."""
    seen = f"that file is {-(-size // 1024)} KB" if size else "that file is bigger than the limit"
    return IconError("too_large", f"{seen}: the most is {MAX_BYTES // 1024} KB")


def _looks_like_svg(data: bytes) -> bool:
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith(b"<?xml") or b"<svg" in head


def _kind(data: bytes) -> str | None:
    if data.startswith(PNG_SIGNATURE):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _be32(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 4], "big")


def _le(data: bytes, at: int, n: int) -> int:
    return int.from_bytes(data[at : at + n], "little")


def png_size(data: bytes) -> tuple[int, int]:
    """The width and height in a PNG's header, after walking every chunk: each one's length fits, its
    type is letters, its CRC is right, IHDR is first and holds a header the standard allows, there is
    pixel data, and IEND is the last byte of the file."""
    bad = IconError(
        "bad_png", "that PNG is damaged or cut short: save it again from the program that made it"
    )
    pos, n = len(PNG_SIGNATURE), len(data)
    width = height = 0
    first, idat = True, False
    while True:
        if pos + 12 > n:
            raise bad
        length, kind = _be32(data, pos), data[pos + 4 : pos + 8]
        end = pos + 8 + length + 4
        if length > 0x7FFFFFFF or end > n or not kind.isalpha():
            raise bad
        if zlib.crc32(data[pos + 4 : end - 4]) != _be32(data, end - 4):
            raise bad
        if first:
            if kind != b"IHDR" or length != 13:
                raise bad
            width, height = _be32(data, pos + 8), _be32(data, pos + 12)
            depth, color, method, filtering, interlace = data[pos + 16 : pos + 21]
            if (
                not width
                or not height
                or depth not in _PNG_DEPTHS.get(color, ())
                or method != 0
                or filtering != 0
                or interlace not in (0, 1)
            ):
                raise bad
            first = False
        elif kind == b"IHDR":
            raise bad
        elif kind == b"IDAT":
            idat = True
        elif kind == b"IEND":
            if length != 0 or not idat:
                raise bad
            if end != n:
                raise IconError(
                    "bad_png", "that PNG has other data after its end: save it again as a plain PNG"
                )
            return width, height
        pos = end


def webp_size(data: bytes) -> tuple[int, int]:
    """The width and height in a WebP's header, for its three layouts: lossless (VP8L), lossy (VP8) and
    extended (VP8X, whose canvas size is the picture's). The RIFF size must be the file's, every chunk
    must fit, with its pad byte when its length is odd, and the first chunk must be one of the three."""
    bad = IconError(
        "bad_webp", "that WebP is damaged or cut short: save it again from the program that made it"
    )
    n = len(data)
    declared = _le(data, 4, 4) + 8
    if declared < n:
        raise IconError("bad_webp", "that WebP has other data after its end: save it again as a plain WebP")
    if declared != n:
        raise bad
    pos = 12
    chunks: list[tuple[bytes, int, int]] = []
    while pos < n:
        if pos + 8 > n:
            raise bad
        kind, size = data[pos : pos + 4], _le(data, pos + 4, 4)
        start = pos + 8
        pos = start + size + (size & 1)
        if pos > n:
            raise bad
        chunks.append((kind, start, size))
    if not chunks:
        raise bad
    kind, start, size = chunks[0]
    if kind == b"VP8X":
        if size < 10 or not {c[0] for c in chunks} & {b"VP8 ", b"VP8L", b"ANMF"}:
            raise bad
        return 1 + _le(data, start + 4, 3), 1 + _le(data, start + 7, 3)
    if kind == b"VP8L":
        bits = _le(data, start + 1, 4)
        if size < 5 or data[start] != 0x2F or bits >> 29:
            raise bad
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8 ":
        if size < 10 or data[start] & 1 or data[start + 3 : start + 6] != b"\x9d\x01\x2a":
            raise bad
        width, height = _le(data, start + 6, 2) & 0x3FFF, _le(data, start + 8, 2) & 0x3FFF
        if not width or not height:
            raise bad
        return width, height
    raise bad


def check(data: bytes, declared: str | None) -> Checked:
    """Judge an upload by its bytes. ``declared`` is the request's Content-Type, which must agree with
    them. Raises ``IconError`` for anything the broker won't store."""
    if not data:
        raise IconError("empty", "that file is empty")
    if len(data) > MAX_BYTES:
        raise too_large(len(data))
    label = (declared or "").split(";", 1)[0].strip().lower()
    actual = _kind(data)
    svg = IconError("svg", "SVG isn't accepted, because it can carry script: use a PNG or a WebP instead")
    if actual is None:
        if _looks_like_svg(data):
            raise svg
        raise IconError("not_an_image", "that file isn't a PNG or a WebP image")
    if label == "image/svg+xml":
        raise svg
    if label not in TYPES:
        sent = f"this was sent as {label}" if label else "the request named no type"
        raise IconError("type", f"only PNG and WebP images are accepted ({sent})")
    if label != actual:
        name = "PNG" if actual == "image/png" else "WebP"
        raise IconError(
            "mismatch", f"that file is a {name} image but was sent as {label}: it must be what it says"
        )
    width, height = png_size(data) if actual == "image/png" else webp_size(data)
    if width > MAX_SIDE or height > MAX_SIDE:
        raise IconError(
            "dimensions", f"that image is {width} × {height} pixels: the most is {MAX_SIDE} × {MAX_SIDE}"
        )
    return Checked(actual, width, height, hashlib.sha256(data).hexdigest())


def icon_json(icon: AgentIcon) -> dict[str, object]:
    """What the settings page is told about one stored avatar: never its bytes."""
    return {
        "harness": icon.harness,
        "scheme": icon.scheme,
        "type": icon.mime,
        "width": icon.width,
        "height": icon.height,
        "bytes": icon.size,
        "v": icon.version,
    }


def url(harness: str, scheme: str, version: str) -> str:
    """The URL of one stored avatar, with its cache key."""
    return f"/api/agent-icons/{harness}/{scheme}?v={version}"


def stylesheet(icons: list[AgentIcon]) -> str:
    """The CSS that puts the uploaded avatars on the page, after ``style.css`` so it wins at equal
    weight. For each harness with an image: the tile shows it (the light one, the dark one under a dark
    scheme or an explicit Dark theme, and either one for both when only one was uploaded), the drawn
    glyph and the word under it are hidden, and the settings page's preview of each stored slot gets its
    image. A harness with none gets no rule, so the built-in avatar is untouched."""
    by_slot = {
        (i.harness, i.scheme): i
        for i in icons
        if i.harness in HARNESSES and i.scheme in SCHEMES and _VERSION_RE.fullmatch(i.version)
    }
    out = ["/* the avatars an admin uploaded (DESIGN.md §29.10), written by the broker */"]
    for h in HARNESSES:
        light, dark = by_slot.get((h, "light")), by_slot.get((h, "dark"))
        if light is None and dark is None:
            continue
        shown_light = light or dark
        shown_dark = dark or light
        assert shown_light is not None and shown_dark is not None
        tile = f".avatar.h-{h}"
        in_light = f"url('{url(h, shown_light.scheme, shown_light.version)}')"
        in_dark = f"url('{url(h, shown_dark.scheme, shown_dark.version)}')"
        out += [
            f"{tile} {{ background-color: transparent; background-image: {in_light};"
            " background-position: center; background-size: contain; background-repeat: no-repeat;"
            " color: transparent; box-shadow: inset 0 0 0 1px var(--h-icon-ring); }",
            f'@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {tile} '
            f"{{ background-image: {in_dark}; }} }}",
            f':root[data-theme="dark"] {tile} {{ background-image: {in_dark}; }}',
            f"{tile} > .ic, {tile} > .av-word {{ display: none; }}",
        ]
        for slot in (light, dark):
            if slot is not None:
                out.append(
                    f".agent-icon-slot.slot-{h}-{slot.scheme} "
                    f"{{ background-image: url('{url(h, slot.scheme, slot.version)}'); }}"
                )
    return "\n".join(out) + "\n"

"""Generated pictures for the avatar upload tests (#498, DESIGN.md §29.11).

Solid-colour PNG and WebP files, made here from nothing: no artwork, and no company's logo, is in
the repository for these tests. The PNGs are ordinary RGBA files (zlib and CRC from the standard
library). The WebPs are lossless (VP8L) files whose five prefix codes each hold one symbol, so a
pixel costs no bits and a browser decodes them as the solid colour; the header-only builders
(``webp_lossy_header``, ``webp_extended``) make files whose bodies are not decodable, for the parser's
own tests. Each ``broken_*`` function takes a valid file and damages it in one named way.
"""

from __future__ import annotations

import random
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(
    width: int = 64,
    height: int = 64,
    rgba: tuple[int, int, int, int] = (200, 30, 30, 255),
    *,
    pad: int = 0,
) -> bytes:
    """A solid-colour 8-bit RGBA PNG. ``pad`` adds that many bytes of random text in an ancillary
    chunk, so a file is valid and large (the size cap's test) without being a large picture."""
    row = b"\x00" + bytes(rgba) * width
    out = PNG_SIGNATURE
    out += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    if pad:
        noise = bytes(random.Random(width * 7919 + pad).randrange(256) for _ in range(pad))
        out += _png_chunk(b"tEXt", b"Comment\x00" + noise)
    out += _png_chunk(b"IDAT", zlib.compress(row * height))
    return out + _png_chunk(b"IEND", b"")


class _Bits:
    """VP8L's bit order: least significant bit first."""

    def __init__(self) -> None:
        self.value = 0
        self.count = 0

    def write(self, value: int, bits: int) -> None:
        self.value |= (value & ((1 << bits) - 1)) << self.count
        self.count += bits

    def bytes(self) -> bytes:
        return self.value.to_bytes((self.count + 7) // 8, "little")


def riff(*chunks: tuple[bytes, bytes]) -> bytes:
    body = b"WEBP"
    for kind, data in chunks:
        body += kind + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) % 2 else b"")
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp(width: int = 64, height: int = 64, rgba: tuple[int, int, int, int] = (30, 30, 200, 255)) -> bytes:
    """A solid-colour lossless WebP: the header, then one single-symbol prefix code each for green,
    red, blue, alpha and distance, so the pixels need no bits."""
    r, g, b, a = rgba
    bits = _Bits()
    bits.write(0x2F, 8)
    bits.write(width - 1, 14)
    bits.write(height - 1, 14)
    bits.write(1 if a != 255 else 0, 1)
    bits.write(0, 3)  # version
    bits.write(0, 1)  # no transform
    bits.write(0, 1)  # no colour cache
    bits.write(0, 1)  # no meta prefix image
    for symbol in (g, r, b, a, 0):
        bits.write(1, 1)  # a simple code
        bits.write(0, 1)  # one symbol
        if symbol < 2:
            bits.write(0, 1)
            bits.write(symbol, 1)
        else:
            bits.write(1, 1)
            bits.write(symbol, 8)
    return riff((b"VP8L", bits.bytes()))


def webp_lossy_header(width: int, height: int) -> bytes:
    """A lossy WebP's container and frame header, with a body no decoder would accept."""
    tag = b"\x10\x00\x00"  # a key frame
    frame = tag + b"\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\x00" * 8
    return riff((b"VP8 ", frame))


def webp_extended(width: int, height: int, *, animated: bool = False) -> bytes:
    """An extended WebP (VP8X) around a lossless image: the canvas size is the header's."""
    flags = 0x02 if animated else 0
    canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    inner = webp(min(width, 16), min(height, 16))[20:]  # the VP8L payload of a small image
    return riff((b"VP8X", bytes([flags]) + b"\x00\x00\x00" + canvas), (b"VP8L", inner))


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64"/></svg>'


# ------------------------------------------------------------------ one way to be broken each
def flip(data: bytes, at: int) -> bytes:
    """``data`` with every bit of the byte at ``at`` flipped."""
    return data[:at] + bytes([data[at] ^ 0xFF]) + data[at + 1 :]


def png_with_size(width: int, height: int) -> bytes:
    """A PNG whose header says ``width`` by ``height`` (CRC kept right) over a body of another size."""
    good = png(8, 8)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return good[:8] + _png_chunk(b"IHDR", ihdr) + good[8 + 25 :]


def png_with_ihdr(**changes: int) -> bytes:
    """A PNG with one IHDR field changed (``depth``, ``color``, ``compression``, ``filter``,
    ``interlace``) and its CRC kept right."""
    fields = {"depth": 8, "color": 6, "compression": 0, "filter": 0, "interlace": 0, **changes}
    ihdr = struct.pack(
        ">IIBBBBB", 8, 8, fields["depth"], fields["color"], fields["compression"], fields["filter"],
        fields["interlace"],
    )  # fmt: skip
    good = png(8, 8)
    return good[:8] + _png_chunk(b"IHDR", ihdr) + good[8 + 25 :]


def png_without_idat() -> bytes:
    good = png(8, 8)
    return good[:33] + _png_chunk(b"IEND", b"")


def png_with_chunk_first(kind: bytes) -> bytes:
    """A PNG whose first chunk is ``kind``, not IHDR."""
    good = png(8, 8)
    return PNG_SIGNATURE + _png_chunk(kind, b"\x00" * 13) + good[8:]


def png_with_overrunning_chunk() -> bytes:
    """A PNG whose IDAT says it is 4 GB long."""
    good = png(8, 8)
    start = good.index(b"IDAT") - 4
    return good[:start] + struct.pack(">I", 0xFFFFFFF0) + good[start + 4 :]


def riff_size(data: bytes, size: int) -> bytes:
    """``data`` with its RIFF size field set to ``size``."""
    return data[:4] + struct.pack("<I", size) + data[8:]

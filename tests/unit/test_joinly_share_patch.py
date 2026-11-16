"""The share_image tool of the joinly patch (#763, DESIGN.md §52.9) takes bytes, never a link.

joinly is not installed here, so the patch's pure helper is read out of the file the container runs and
exercised alone: the file is parsed, ``_share_picture`` is pulled out of it, and nothing else is executed.
"""

from __future__ import annotations

import ast
import base64
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

SERVER = Path(__file__).resolve().parents[2] / "deploy" / "joinly" / "server.py"


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def _jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0\x00\x0b\x08" + struct.pack(">HH", height, width) + b"\x01\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


@pytest.fixture(scope="module")
def helper() -> Any:
    """``_share_picture`` and the constants it reads, compiled alone from the patch's source."""
    tree = ast.parse(SERVER.read_text())
    wanted = {"SHARE_MAX_BYTES", "SHARE_MAX_PIXELS", "_JPEG_FRAMES", "_share_picture"}
    keep: list[ast.stmt] = []
    for node in tree.body:
        names = {t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)}
        if isinstance(node, ast.FunctionDef) and node.name in wanted or names & wanted:
            keep.append(node)
    scope: dict[str, Any] = {"base64": base64}
    exec(compile(ast.Module(keep, []), str(SERVER), "exec"), scope)  # noqa: S102
    return scope["_share_picture"]


def test_a_png_and_a_jpeg_become_data_urls_of_their_own_type(helper: Any) -> None:
    assert helper(_png(40, 30)).startswith("data:image/png;base64,")
    assert helper(_jpeg(40, 30)).startswith("data:image/jpeg;base64,")
    assert base64.b64decode(helper(_png(2, 2)).split(",", 1)[1]) == _png(2, 2)


@pytest.mark.parametrize(
    "data",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32,
        b"GIF89a" + b"\x00" * 32,
        b"https://example.com/picture.png",  # a link is not a picture
        b"",
    ],
)
def test_anything_else_is_refused_by_its_bytes(helper: Any, data: bytes) -> None:
    with pytest.raises(ValueError, match="PNG or JPEG"):
        helper(data)


def test_a_picture_that_claims_too_many_pixels_is_refused_before_the_browser_sees_it(helper: Any) -> None:
    """A tiny PNG can claim 60000 by 60000 pixels: decoding it would use the container's memory."""
    with pytest.raises(ValueError, match="too large"):
        helper(_png(60000, 60000))
    with pytest.raises(ValueError, match="too large"):
        helper(_jpeg(60000, 60000))
    with pytest.raises(ValueError, match="size could not be read"):
        helper(b"\xff\xd8\xff\xe0\x00\x02")  # a JPEG with no frame header


def test_a_body_over_the_byte_cap_is_refused(helper: Any) -> None:
    with pytest.raises(ValueError, match="too large"):
        helper(_png(2, 2) + b"\x00" * (8 * 1024 * 1024))


def test_the_tools_take_no_url_and_never_use_the_browsers_own_screen_picker() -> None:
    """``share_image``'s only argument is the picture's text; the page is given the canvas through the
    override and nothing else. Fails if a ``url`` argument is added, or the override is dropped for the
    flag-driven picker path, which would capture the bot's whole virtual display."""
    tree = ast.parse(SERVER.read_text())
    tools = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    args = [a.arg for a in tools["share_image"].args.args]
    assert args == ["ctx", "image"]
    assert [a.arg for a in tools["stop_share_image"].args.args] == ["ctx"]
    source = SERVER.read_text()
    share_part = source[source.index("_SHARE_JS = ") : source.index('"mute_yourself"')]
    assert "md.getDisplayMedia = async" in share_part  # the canvas override
    assert "goto(" not in share_part and "new_page" not in share_part  # no page is ever opened for a share
    assert "displaySurface" not in share_part and "selfBrowserSurface" not in share_part

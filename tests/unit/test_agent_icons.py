"""Checking an uploaded avatar (DESIGN.md §29.10, #498): what a file must be before the broker stores it
and sends it back to every browser, and the stylesheet that shows it.

The checks read the file's own bytes: its magic number, its parsed size and, for a PNG, every chunk's
CRC, so a file that is cut short, damaged, or a picture with something appended is refused, and one
that says it is a PNG but is a WebP (or an SVG, which can carry script) is refused with a message that
says so. Nothing here decodes pixels, and nothing needs Pillow: the server image has none."""

from __future__ import annotations

import re

import pytest
from fakes import pictures

from switchboard.broker import agent_icons as ai
from switchboard.broker.agent_icons import IconError, check
from switchboard.models import AgentIcon

PNG = pictures.png(64, 48)
WEBP = pictures.webp(64, 48)


def refused(data: bytes, declared: str | None, code: str) -> IconError:
    with pytest.raises(IconError) as e:
        check(data, declared)
    assert e.value.code == code, (e.value.code, e.value.message)
    assert e.value.message and e.value.status in (400, 413, 415)
    return e.value


# ------------------------------------------------------------------ what is accepted
def test_a_png_and_a_webp_are_accepted_with_their_parsed_size() -> None:
    """The size comes from the file, never from the sender: a valid file of each kind passes, and what
    is stored is what was parsed. Fails if either parser misreads a size or a type."""
    got = check(PNG, "image/png")
    assert (got.mime, got.width, got.height) == ("image/png", 64, 48)
    assert len(got.sha256) == 64 and got.version == got.sha256[:12]
    got = check(WEBP, "image/webp")
    assert (got.mime, got.width, got.height) == ("image/webp", 64, 48)


@pytest.mark.parametrize("side", [1, 16, 511, 512])
def test_every_size_up_to_the_cap_passes_in_both_formats(side: int) -> None:
    assert check(pictures.png(side, side), "image/png").width == side
    assert check(pictures.webp(side, side), "image/webp").height == side


def test_a_content_type_with_parameters_or_capitals_is_read_as_its_type() -> None:
    """A browser may send ``image/png; charset=binary`` or ``Image/PNG``: the type is the part before
    the semicolon, lowercased."""
    assert check(PNG, "Image/PNG").mime == "image/png"
    assert check(PNG, "image/png; charset=binary").mime == "image/png"


def test_the_other_two_webp_layouts_are_read_too() -> None:
    """A lossy WebP (VP8) and an extended one (VP8X, whose canvas size is in its header) are WebP: a
    person's export tool may make either. Fails if a layout is refused or its size is read wrong."""
    lossy = check(pictures.webp_lossy_header(300, 200), "image/webp")
    assert (lossy.width, lossy.height) == (300, 200)
    extended = check(pictures.webp_extended(480, 96), "image/webp")
    assert (extended.width, extended.height) == (480, 96)
    animated = check(pictures.webp_extended(64, 64, animated=True), "image/webp")
    assert animated.width == 64


def test_the_version_follows_the_bytes_and_nothing_else() -> None:
    """The cache key of an avatar is its content's hash: the same file twice gives the same key, and
    a different file a different one, so a new upload always changes the URL."""
    again = check(pictures.png(64, 48), "image/png")
    other = check(pictures.png(64, 48, (1, 2, 3, 255)), "image/png")
    assert again.version == check(PNG, "image/png").version
    assert other.version != again.version
    assert re.fullmatch(r"[0-9a-f]{12}", other.version)


# ------------------------------------------------------------------ size and dimension caps
def test_an_empty_body_is_refused() -> None:
    assert "empty" in refused(b"", "image/png", "empty").message


def test_a_file_over_the_byte_cap_is_refused_before_it_is_read_as_a_picture() -> None:
    """256 KB is the most; the limit counts bytes of the file, not pixels. A valid PNG padded past it
    is refused with the size and the limit in the message (HTTP 413)."""
    big = pictures.png(64, 64, pad=ai.MAX_BYTES)
    assert len(big) > ai.MAX_BYTES
    e = refused(big, "image/png", "too_large")
    assert e.status == 413 and "256 KB" in e.message
    exact = pictures.png(64, 64, pad=ai.MAX_BYTES - len(pictures.png(64, 64, pad=1)) + 1)
    assert len(exact) == ai.MAX_BYTES and check(exact, "image/png").width == 64  # the cap is inclusive


@pytest.mark.parametrize(("w", "h"), [(513, 64), (64, 513), (4096, 4096)])
def test_a_picture_over_512_pixels_either_way_is_refused(w: int, h: int) -> None:
    """The browser decodes what is sent, so the pixel count is bounded by the header: one side past 512
    is refused and the message gives the picture's size and the limit."""
    for data, mime in ((pictures.png_with_size(w, h), "image/png"), (pictures.webp(w, h), "image/webp")):
        e = refused(data, mime, "dimensions")
        assert f"{w} × {h}" in e.message and "512 × 512" in e.message


def test_a_picture_with_a_zero_side_is_refused() -> None:
    refused(pictures.png_with_size(0, 8), "image/png", "bad_png")
    refused(pictures.png_with_size(8, 0), "image/png", "bad_png")


# ------------------------------------------------------------------ type, label and SVG
def test_svg_is_refused_whatever_it_is_called_and_the_message_says_why() -> None:
    """SVG can carry script, so it is never stored: a file labelled image/svg+xml, one that is SVG
    text labelled PNG or WebP, and one with an XML prolog or a BOM in front are all refused with the
    SVG message. Fails if any of them reaches the store."""
    for declared in ("image/svg+xml", "image/png", "image/webp", None, "text/xml"):
        e = refused(pictures.SVG, declared, "svg")
        assert "SVG" in e.message and "script" in e.message and e.status == 415
    prolog = b'<?xml version="1.0"?>\n' + pictures.SVG
    refused(prolog, "image/png", "svg")
    refused(b"\xef\xbb\xbf  \n" + pictures.SVG, "image/png", "svg")
    refused(b"<!DOCTYPE svg PUBLIC...>" + pictures.SVG, "image/webp", "svg")
    refused(PNG, "image/svg+xml", "svg")  # even real PNG bytes, when the sender calls it SVG


@pytest.mark.parametrize("declared", ["image/jpeg", "image/gif", "text/html", "application/octet-stream", ""])
def test_any_other_type_is_refused(declared: str) -> None:
    e = refused(PNG, declared, "type")
    assert "PNG" in e.message and "WebP" in e.message and e.status == 415


def test_no_content_type_is_refused() -> None:
    refused(PNG, None, "type")


def test_a_png_called_webp_and_a_webp_called_png_are_refused_with_what_each_is() -> None:
    """A mislabelled file is refused rather than trusted either way, and the message names both the
    label and the file's real type."""
    e = refused(PNG, "image/webp", "mismatch")
    assert "PNG" in e.message and "image/webp" in e.message
    e = refused(WEBP, "image/png", "mismatch")
    assert "WebP" in e.message and "image/png" in e.message


@pytest.mark.parametrize(
    "data",
    [
        b"GIF89a" + b"\x00" * 40,
        b"\xff\xd8\xff\xe0" + b"\x00" * 40,
        b"<html><script>1</script></html>",
        b"x" * 100,
    ],
)
def test_bytes_that_are_neither_a_png_nor_a_webp_are_refused(data: bytes) -> None:
    for declared in ("image/png", "image/webp"):
        e = refused(data, declared, "not_an_image")
        assert "PNG" in e.message and "WebP" in e.message


def test_a_png_with_markup_appended_is_refused() -> None:
    """A polyglot (a picture followed by HTML, a script or a zip) is no avatar: everything after the
    PNG's last chunk is refused."""
    refused(PNG + b"<script>alert(1)</script>", "image/png", "bad_png")
    refused(PNG + b"\x00", "image/png", "bad_png")


# ------------------------------------------------------------------ damaged PNGs
def test_no_cut_of_a_png_passes() -> None:
    """Every shorter prefix of a valid PNG is refused (the sender's connection dropped, or the file
    was copied half way): the parser walks to IEND and fails on any file that stops before it."""
    small = pictures.png(8, 8)
    for n in range(1, len(small)):
        with pytest.raises(IconError):
            check(small[:n], "image/png")


def test_no_cut_of_a_webp_passes() -> None:
    small = pictures.webp(8, 8)
    for n in range(1, len(small)):
        with pytest.raises(IconError):
            check(small[:n], "image/webp")


def test_a_flipped_byte_anywhere_in_a_png_is_refused() -> None:
    """Every chunk's CRC is checked, so one flipped byte in the signature, a length, a type, the data
    or a CRC is caught. Fails if a chunk's CRC is skipped."""
    small = pictures.png(8, 8)
    for at in range(len(small)):
        with pytest.raises(IconError):
            check(pictures.flip(small, at), "image/png")


def test_a_png_without_ihdr_first_or_without_pixels_is_refused() -> None:
    refused(pictures.png_with_chunk_first(b"IDAT"), "image/png", "bad_png")
    refused(pictures.png_with_chunk_first(b"tEXt"), "image/png", "bad_png")
    refused(pictures.png_without_idat(), "image/png", "bad_png")


def test_a_chunk_that_says_it_is_longer_than_the_file_is_refused() -> None:
    refused(pictures.png_with_overrunning_chunk(), "image/png", "bad_png")


@pytest.mark.parametrize(
    "change",
    [
        {"color": 1},
        {"color": 5},
        {"color": 7},
        {"depth": 3},
        {"depth": 16, "color": 3},
        {"depth": 4, "color": 2},
        {"compression": 1},
        {"filter": 1},
        {"interlace": 2},
    ],
)
def test_a_png_header_that_no_decoder_accepts_is_refused(change: dict[str, int]) -> None:
    """The colour type, the depth that colour type allows, and the three method bytes are all in the
    header, and a combination the PNG standard doesn't define is no PNG."""
    refused(pictures.png_with_ihdr(**change), "image/png", "bad_png")


def test_a_png_with_the_header_values_the_standard_allows_passes() -> None:
    for change in ({"color": 0, "depth": 1}, {"color": 3, "depth": 4}, {"color": 4}, {"interlace": 1}):
        assert check(pictures.png_with_ihdr(**change), "image/png").width == 8


# ------------------------------------------------------------------ damaged WebPs
def test_a_riff_size_that_does_not_match_the_file_is_refused() -> None:
    """The RIFF header says how long the file is: longer than the file means it was cut short, shorter
    means something was appended."""
    refused(pictures.riff_size(WEBP, len(WEBP) - 8 + 2), "image/webp", "bad_webp")
    refused(pictures.riff_size(WEBP, len(WEBP) - 8 - 2), "image/webp", "bad_webp")
    refused(WEBP + b"<script>", "image/webp", "bad_webp")


def test_a_webp_with_the_wrong_signature_or_first_chunk_is_refused() -> None:
    refused(b"RIFX" + WEBP[4:], "image/webp", "not_an_image")
    refused(WEBP[:8] + b"WAVE" + WEBP[12:], "image/webp", "not_an_image")
    refused(WEBP[:12] + b"JUNK" + WEBP[16:], "image/webp", "bad_webp")
    refused(pictures.riff((b"VP8X", b"\x00" * 4)), "image/webp", "bad_webp")  # too short for a canvas


def test_a_vp8l_header_with_the_wrong_signature_or_version_is_refused() -> None:
    """The lossless header starts 0x2f and has a three-bit version that must be 0."""
    assert WEBP[20] == 0x2F
    refused(pictures.flip(WEBP, 20), "image/webp", "bad_webp")
    version_byte = 24  # the top three bits of the fourth header byte
    refused(
        WEBP[:version_byte] + bytes([WEBP[version_byte] | 0x20]) + WEBP[version_byte + 1 :],
        "image/webp",
        "bad_webp",
    )


def test_a_lossy_header_needs_a_key_frame_and_its_start_code() -> None:
    lossy = pictures.webp_lossy_header(64, 64)
    frame = lossy.index(b"VP8 ") + 8
    refused(lossy[:frame] + b"\x11" + lossy[frame + 1 :], "image/webp", "bad_webp")  # an inter frame
    refused(
        lossy[: frame + 3] + b"\x00\x00\x00" + lossy[frame + 6 :], "image/webp", "bad_webp"
    )  # no start code
    refused(pictures.webp_lossy_header(0, 64), "image/webp", "bad_webp")


def test_an_odd_sized_chunk_must_be_padded() -> None:
    """RIFF pads a chunk of odd length with a byte: a file that leaves it out is cut or damaged."""
    odd = (
        b"RIFF"
        + (4 + 8 + 9).to_bytes(4, "little")
        + b"WEBP"
        + b"VP8L"
        + (9).to_bytes(4, "little")
        + b"\x2f"
        + b"\x00" * 8
    )
    refused(odd, "image/webp", "bad_webp")


def test_an_extended_webp_without_an_image_in_it_is_refused() -> None:
    extended = pictures.webp_extended(64, 64)
    no_image = b"RIFF" + (4 + 8 + 10).to_bytes(4, "little") + b"WEBP" + extended[12 : 12 + 8 + 10]
    refused(no_image, "image/webp", "bad_webp")


# ------------------------------------------------------------------ the stylesheet
def rows(*pairs: tuple[str, str, str]) -> list[AgentIcon]:
    """Stored icons (no bytes): ``v`` repeated is the hash, so the URL has that letter twelve times."""
    return [AgentIcon(h, s, "image/png", 64, 64, 100, v * 64, 1.0) for h, s, v in pairs]


def test_no_custom_icons_make_a_stylesheet_that_changes_nothing() -> None:
    css = ai.stylesheet([])
    assert "{" not in css and "url(" not in css


def test_a_harness_with_both_images_gets_the_light_one_by_default_and_the_dark_one_in_dark() -> None:
    css = ai.stylesheet(rows(("codex", "light", "a"), ("codex", "dark", "b")))
    assert ".avatar.h-codex {" in css
    assert "url('/api/agent-icons/codex/light?v=aaaaaaaaaaaa')" in css
    assert ':root[data-theme="dark"] .avatar.h-codex' in css
    assert '@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) .avatar.h-codex' in css
    dark_blocks = [b for b in css.split("\n") if "bbbbbbbbbbbb" in b and ".avatar.h-codex" in b]
    assert len(dark_blocks) == 2  # the system-dark rule and the explicit Dark one


def test_one_image_is_used_for_both_schemes() -> None:
    """If an admin uploads only a light image, dark mode shows it too (and the other way round):
    mixing a custom icon with the built-in one for the other scheme would look broken."""
    only_light = ai.stylesheet(rows(("cursor", "light", "c")))
    assert "dddddddddddd" not in only_light
    assert only_light.count("v=cccccccccccc") >= 3  # base, system dark and explicit dark rules
    assert "cursor/dark" not in only_light
    only_dark = ai.stylesheet(rows(("cursor", "dark", "d")))
    assert only_dark.count("v=dddddddddddd") >= 3 and "cursor/light" not in only_dark


def test_a_custom_icon_hides_the_built_in_glyph_and_the_word_under_it() -> None:
    """Claude Code's built-in avatar is a drawn glyph (and, on the Inspector's, the word Claude): with
    an uploaded image on the tile neither may show over it."""
    css = ai.stylesheet(rows(("claude", "light", "e")))
    assert ".avatar.h-claude > .ic, .avatar.h-claude > .av-word { display: none; }" in css
    assert "background-color: transparent" in css


def test_the_settings_previews_are_the_stored_slots_not_the_fallback() -> None:
    css = ai.stylesheet(rows(("devin", "light", "f")))
    assert ".agent-icon-slot.slot-devin-light {" in css and "slot-devin-dark" not in css


def test_the_stylesheet_has_only_known_names_and_one_shape_of_url() -> None:
    """Whatever the rows hold, every url() in the stylesheet is ``/api/agent-icons/<harness>/<scheme>?v=``
    and twelve hex digits, and nothing else loads or imports anything. A row with a name outside the four
    harnesses (it can't be in the database: its CHECK refuses it) is left out rather than written."""
    every = [(h, s, "9") for h in ai.HARNESSES for s in ai.SCHEMES]
    css = ai.stylesheet(rows(*every, ("nope", "light", "9")))
    urls = re.findall(r"url\(([^)]*)\)", css)
    assert urls and all(
        re.fullmatch(r"'/api/agent-icons/(claude|codex|cursor|devin)/(light|dark)\?v=[0-9a-f]{12}'", u)
        for u in urls
    )
    assert (
        "nope" not in css
        and "@import" not in css
        and "</style" not in css.lower()
        and "expression" not in css
    )
    assert css.count("{") == css.count("}")

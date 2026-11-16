"""The pure side of showing a room's picture in a call (#763, DESIGN.md §52.9).

What may be shared is decided by the bytes: a PNG or a JPEG, under a cap. Nothing here sleeps or starts a
process.
"""

from __future__ import annotations

import base64

import pytest

from switchboard.meeting import share
from switchboard.meeting.share import ShareError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (PNG, "png"),
        (JPEG, "jpg"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", None),  # a WebP is a snapshot's kind, not a share's
        (b"GIF89a....", None),
        (b"<svg></svg>", None),
        (b"%PDF-1.7", None),
        (b"", None),
        (b"\x89PNG", None),  # a truncated signature
    ],
)
def test_the_kind_is_read_from_the_bytes_alone(data: bytes, kind: str | None) -> None:
    assert share.picture_kind(data) == kind


def test_a_picture_over_the_smaller_of_the_two_caps_is_refused() -> None:
    """The room's file limit and the patch's cap both apply; the lower wins. Fails if a limit above the
    patch's cap lets a picture through that joinly would refuse, or one below it is ignored."""
    assert share.check_picture(PNG, 25 * 2**20) == "png"
    with pytest.raises(ShareError, match="larger"):
        share.check_picture(PNG + b"\x00" * share.SHARE_MAX_BYTES, 100 * 2**20)
    with pytest.raises(ShareError, match="larger"):
        share.check_picture(PNG, len(PNG) - 1)
    with pytest.raises(ShareError, match="PNG or JPEG"):
        share.check_picture(b"text", 2**20)


def test_a_picture_travels_as_base64_text_and_comes_back_the_same() -> None:
    assert base64.b64decode(share.encode(PNG), validate=True) == PNG


def test_a_reference_is_the_words_after_share_joined() -> None:
    assert share.reference(("release", "plan.png")) == "release plan.png"
    for words in ((), ("  ",)):
        with pytest.raises(ShareError, match="usage"):
            share.reference(words)
    with pytest.raises(ShareError, match="too long"):
        share.reference(("x" * (share.SHARE_NAME_MAX + 1),))


def test_a_file_id_is_recognized_in_its_two_spellings_and_nothing_else() -> None:
    fid = "ab" * 16
    assert share.file_id_of(fid) == fid
    assert share.file_id_of(f"switchboard:file/{fid}") == fid
    assert share.file_id_of(fid.upper()) == fid
    for other in ("plan.png", fid + "0", f"https://example.com/api/files/{fid}", f"switchboard:file/{fid}/x"):
        assert share.file_id_of(other) is None


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ("Host disabled attendee screen sharing.", True),
        ("Error calling tool 'share_image': HOST DISABLED attendee screen sharing", True),
        ("Zoom did not start the share.", False),
        ("The Share button was not found in the call.", False),
        ("", False),
    ],
)
def test_the_hosts_refusal_is_told_from_other_failures(text: str, refused: bool) -> None:
    assert share.host_refused(text) is refused

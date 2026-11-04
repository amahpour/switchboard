"""Speaking in a call and seeing its screen: the pure parts (#593, DESIGN.md §52).

What an agent's ``@call`` line says aloud, which messages are one, the picture check, which
call lines ask for a snapshot, the voice setting and the reserved name. Nothing here reads a clock
or opens a socket.
"""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from switchboard import db
from switchboard.clock import SystemClock
from switchboard.config import Config, ConfigError, FilesCfg, MeetingCfg, from_dict
from switchboard.files import Attachments, FolderStore
from switchboard.meeting.names import hotwords
from switchboard.meeting.screen import NoFrame, frame_from, image_kind, says_screen, wants_snapshot
from switchboard.meeting.speech import SPEAK_MAX, call_text, speakable
from switchboard.models import RESERVED_NAMES
from switchboard.store import Store

# ---------------------------------------------------------------- @call


@pytest.mark.parametrize(
    ("text", "rest"),
    [
        ("@call the build is green", "the build is green"),
        ("@CALL the build is green", "the build is green"),
        ("  @call: the build is green", "the build is green"),
        ("@call, the build is green", "the build is green"),
        ("@call\nthe build is green", "the build is green"),
        ("@call", ""),
    ],
)
def test_a_message_that_starts_with_at_call_gives_what_follows(text: str, rest: str) -> None:
    """Fails if the prefix is case-sensitive, or the punctuation after it is read aloud."""
    assert call_text(text) == rest


@pytest.mark.parametrize(
    "text",
    [
        "the build is green @call",
        "tell @call that it is green",
        "@calls are great",
        "@call-1 take this",
        "@call_me maybe",
        "call the build green",
        "",
    ],
)
def test_anything_else_is_only_text(text: str) -> None:
    """``@call`` must be the first word and the whole word. Fails if a later mention, a longer
    name or a plain word starts speech: that would speak a line nobody meant for the call."""
    assert call_text(text) is None


# ------------------------------------------------------------ speakable


def test_markdown_marks_go_and_the_words_stay() -> None:
    """Emphasis, headings, quotes, list marks and code ticks are not read. A name with an
    underscore keeps it. Fails if the voice would say "asterisk" or "backtick"."""
    text = "# Done\n**All** the _tests_ pass, `make check` too.\n> a quote\n- first\n- second\nsee some_name"
    spoken = "Done. All the tests pass, make check too. a quote. first. second. see some_name"
    assert speakable(text) == spoken


def test_a_link_keeps_its_words_and_a_bare_url_is_left_out() -> None:
    """Fails if an address is read out letter by letter, or a link loses what it says."""
    text = "Read [the report](https://example.com/r?id=1) and https://example.com/x or www.example.com now."
    assert speakable(text) == "Read the report and or now."


def test_an_image_keeps_its_description_and_a_tag_is_dropped() -> None:
    assert speakable("![a chart](https://example.com/c.png) <b>bold</b> text") == "a chart bold text"


def test_a_fenced_code_block_becomes_code_omitted() -> None:
    """Code is not read aloud, but the listener is told there was some. Fails if code is
    spoken, or if the words after the block are lost."""
    text = "Here is the fix:\n```python\nprint('hello')\nx = 1\n```\nThat is all."
    assert speakable(text) == "Here is the fix: code omitted. That is all."


def test_a_tilde_fence_and_an_unclosed_fence_are_code_too() -> None:
    assert speakable("~~~\nrm -rf /\n~~~") == "code omitted"
    assert speakable("before\n```\nnever closed\nstill code") == "before. code omitted"


def test_an_artifact_line_is_not_read() -> None:
    """The line names a path on someone's machine. Fails if the path is spoken in the call."""
    text = "The report is ready.\nartifact: report.pdf | 12 KB | /home/someone/report.pdf"
    assert speakable(text) == "The report is ready."


def test_control_characters_and_blank_text_are_dropped() -> None:
    assert speakable("hel\x1b[31mlo\x00 there") == "hel[31mlo there"
    assert speakable("   \n\n  ") == ""
    assert speakable("https://example.com/only-a-link") == ""
    assert speakable("---") == ""


def test_a_long_message_is_cut_at_a_word_and_ends_with_a_full_stop() -> None:
    """About 600 characters. Fails if the cut falls inside a word, or runs past the limit."""
    words = " ".join(f"word{n}" for n in range(300))
    spoken = speakable(words)
    assert len(spoken) <= SPEAK_MAX + 1
    assert spoken.endswith(".")
    assert spoken[:-1].split(" ")[-1] in words.split(" ")  # whole words only
    assert speakable("short and sweet") == "short and sweet"
    exact = "a" * SPEAK_MAX
    assert speakable(exact) == exact


def test_a_cut_does_not_leave_a_dangling_comma() -> None:
    text = "alpha beta, " + "x" * 10
    assert speakable(text, limit=11) == "alpha beta."


# ------------------------------------------------------ the picture check


def png() -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))


def test_the_kind_is_read_from_the_bytes() -> None:
    """PNG, JPEG and WebP by their first bytes. Fails if the check accepts another file type,
    or trusts a claimed one (there is no claimed type in the call)."""
    assert image_kind(png()) == "png"
    assert image_kind(b"\xff\xd8\xff\xe0" + b"\x00" * 20) == "jpg"
    assert image_kind(b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 20) == "webp"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"GIF89a" + b"\x00" * 20,
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        b"<html><script>alert(1)</script></html>",
        b"%PDF-1.7 " + b"\x00" * 20,
        b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20,
        b"\x89PNG",
        base64.b64encode(png()),
    ],
)
def test_anything_that_is_not_a_picture_has_no_kind(data: bytes) -> None:
    """A GIF, an SVG, HTML, a PDF, a WAV in a RIFF box, a cut-off PNG signature, and a picture
    still in base64 are all refused."""
    assert image_kind(data) is None


def result_of(data: bytes, mime: str = "image/png") -> SimpleNamespace:
    """What fastmcp hands back for ``get_video_snapshot``: content blocks, one an image."""
    block = SimpleNamespace(type="image", data=base64.b64encode(data).decode(), mimeType=mime)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok"), block])


def test_the_frame_is_decoded_and_typed_by_its_bytes() -> None:
    """The claimed ``mimeType`` is ignored both ways. Fails if a PNG claimed as text is refused, or
    HTML claimed as a PNG is kept."""
    assert frame_from(result_of(png(), "text/plain"), 1 << 20) == (png(), "png")
    with pytest.raises(NoFrame, match="not a PNG, JPEG or WebP"):
        frame_from(result_of(b"<html></html>", "image/png"), 1 << 20)


def test_a_frame_over_the_limit_is_refused_before_it_is_decoded() -> None:
    """The size is read off the base64 text, so a huge reply costs nothing. Fails if the cap is
    off by a factor (base64 is a third bigger) or only applied after decoding."""
    exact = png() + b"\x00" * (1000 - len(png()))
    assert frame_from(result_of(exact), 1000)[0] == exact
    with pytest.raises(NoFrame, match="larger than"):
        frame_from(result_of(exact + b"\x00" * 20), 1000)
    huge = SimpleNamespace(content=[SimpleNamespace(type="image", data="!" * 5000, mimeType="image/png")])
    with pytest.raises(NoFrame, match="larger than"):
        frame_from(huge, 1000)  # not base64 at all, but too big comes first


@pytest.mark.parametrize(
    "content",
    [
        [],
        [SimpleNamespace(type="text", text="no frame")],
        [SimpleNamespace(type="image", data=None, mimeType="image/png")],
        [SimpleNamespace(type="image", data="not base64 !!", mimeType="image/png")],
    ],
)
def test_a_reply_without_a_usable_picture_is_refused(content: list[SimpleNamespace]) -> None:
    with pytest.raises(NoFrame):
        frame_from(SimpleNamespace(content=content), 1 << 20)


def attachments(tmp_path: Path, **files: int) -> tuple[Attachments, int]:
    """A real store and folder, with the file caps ``files`` names. Returns it and a room id."""
    store = Store(db.open_db(tmp_path / "sb.db"), SystemClock())
    room = store.create_room("#build", "alice", 60, 30)
    cfg = Config(human_name="alice", files=FilesCfg(**files))
    folder = FolderStore(tmp_path / "files")
    att = Attachments(store, folder, SystemClock(), cfg, "http://switchboard.localhost")
    att.ensure()
    return att, room.id


def test_a_snapshot_is_stored_as_the_callers_file_and_the_caps_apply(tmp_path: Path) -> None:
    """A snapshot is kept like a person's upload (§53): the row says who asked, the bytes are in the
    folder. It stops at the one-file cap and at the room's total. Fails if it skips either cap, or
    stores a rejected picture."""
    att, room = attachments(tmp_path, max_mb=1, room_mb=1)
    mb = 1 << 20
    row = att.store_snapshot(room, "alice", "human", "screen-1.png", png())
    assert (row.uploader_kind, row.uploader_name, row.state) == ("human", "alice", "stored")
    assert row.size == len(png())
    assert att.folder.path(row.id) is not None
    call = att.store_snapshot(room, "call", "meeting", "screen-2.png", png())
    assert call.uploader_kind == "meeting"
    with pytest.raises(ValueError, match="larger than 1 MB"):
        att.store_snapshot(room, "alice", "human", "big.png", png() + b"\x00" * mb)
    fills = att.store_snapshot(room, "alice", "human", "fills.png", png() + b"\x00" * (mb - 4 * len(png())))
    with pytest.raises(ValueError, match="would pass 1 MB"):
        att.store_snapshot(room, "alice", "human", "over.png", png() + b"\x00" * 4096)
    # three files and nothing else: a refused picture leaves no half-written file behind
    assert sorted(p.name for p in att.folder.root.iterdir()) == sorted([row.id, call.id, fills.id])


# ------------------------------------------------ a call-out asking for a screen


def test_screen_is_a_whole_word() -> None:
    """Fails if "screenshot" or "screening" would take a snapshot."""
    assert says_screen("look at my screen")
    assert says_screen("Look at the Screen, please")
    assert says_screen("the screen's blank")
    assert says_screen("screen share")
    assert not says_screen("take a screenshot")
    assert not says_screen("a screening of the film")
    assert not says_screen("screenshare")


MEMBERS = ["claude-1", "codex-1"]


def test_a_snapshot_needs_a_call_out_and_the_word_screen() -> None:
    """Both halves are needed. Fails if talk about a screen with no agent addressed takes a
    picture, or a call-out without the word does."""
    assert wants_snapshot("Claude one, look at my screen", MEMBERS)
    assert wants_snapshot("hey Codex, can you see my screen?", MEMBERS)
    assert not wants_snapshot("look at my screen", MEMBERS)
    # a mention of an agent, not an address to it
    assert not wants_snapshot("my screen is frozen, Claude one is on it", MEMBERS)
    assert not wants_snapshot("Claude one, run the tests", MEMBERS)
    assert not wants_snapshot("I asked Claude one yesterday about the screen", MEMBERS)


def test_an_ambiguous_name_takes_no_snapshot() -> None:
    """Two Claudes and a bare "Claude" addresses nobody (the room asks for the number), so no
    picture is taken for it."""
    assert not wants_snapshot("Claude, look at my screen", ["claude-1", "claude-2"])
    assert wants_snapshot("Claude two, look at my screen", ["claude-1", "claude-2"])


def test_hotwords_are_untouched_by_the_voice() -> None:
    """The names the transcriber is told are the same as before this change."""
    assert "Claude one" in hotwords(["claude-1"])


# -------------------------------------------------------------- the voice


def test_the_voice_defaults_to_george_and_takes_a_short_word() -> None:
    assert MeetingCfg().tts_voice == "bm_george"
    assert from_dict({"meeting": {"tts_voice": "af_bella"}}).meeting.tts_voice == "af_bella"
    assert from_dict({}).meeting.tts_voice == "bm_george"


@pytest.mark.parametrize(
    "voice",
    ["", "BM_GEORGE", "bm george", "bm-george", "bm_george;rm", "x" * 33, "../etc", "voice\n", "é"],
)
def test_a_voice_that_is_not_a_short_word_is_refused(voice: str) -> None:
    """The voice goes into joinly's settings. Fails if anything but a-z, 0-9 and _ (1 to 32
    characters) reaches the header."""
    with pytest.raises(ConfigError, match="tts_voice"):
        from_dict({"meeting": {"tts_voice": voice}})


def test_a_voice_that_is_not_a_string_is_refused() -> None:
    with pytest.raises(ConfigError, match="tts_voice"):
        from_dict({"meeting": {"tts_voice": 5}})


# ------------------------------------------------------- the reserved name


def test_call_is_a_reserved_name() -> None:
    """``@call`` must never be a member's name, or a message to that agent would also be a
    spoken line. Fails if it is left off the reserved list."""
    assert "call" in RESERVED_NAMES

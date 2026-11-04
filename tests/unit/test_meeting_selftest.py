"""The meeting self-test's pure parts, without a call (DESIGN.md §12.4, §52).

``tests/live/test_meeting_selftest.py`` joins a real call with two joinly bots and runs only on request.
What decides whether a step passed, what keeps the call's link out of every output, and what the report
holds is in ``tests/live/harness/selftest.py`` and is checked here on every run. Each test names the mistake
that would make it fail.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_rules_release import item

from switchboard import envelope

LIVE = Path(__file__).resolve().parents[1] / "live"
sys.path.insert(0, str(LIVE))

from harness import selftest  # noqa: E402

# Made up, in the shape of a Zoom invitation: a meeting id and a passcode.
ID = "98765432101"
PASSCODE = "Zq9PasscodeXk4"
LINK = f"https://us02web.zoom.us/j/{ID}?pwd={PASSCODE}"
MEET_LINK = "https://meet.google.com/abc-defg-hij"


# ----------------------------------------------------------------------- words
def test_words_ignore_case_punctuation_and_apostrophes_and_spell_small_numbers() -> None:
    """Speech-to-text writes "Claude 1," or "claude one" and "can't" or "cant" for the same speech. Fails if
    case, a comma, a curly apostrophe or a digit makes two spellings of one thing differ."""
    assert selftest.words("Claude 1, can you HEAR me?") == ["claude", "one", "can", "you", "hear", "me"]
    assert selftest.words("claude-1") == ["claude", "one"]
    assert selftest.words("Can’t stop") == selftest.words("cant stop") == ["cant", "stop"]
    assert selftest.words("Café au lait") == ["cafe", "au", "lait"]


def test_numbers_past_twenty_stay_digits() -> None:
    """Only one to twenty are the names agents have ("claude one"). Fails if 21 becomes a word nobody
    expects, or 20 and 0 are left as digits."""
    assert selftest.words("0 1 20 21 100") == ["zero", "one", "twenty", "21", "100"]


def test_small_words_are_not_content() -> None:
    """ "can you hear me" is compared on "hear". Fails if the small words count, which would let any
    sentence with "the" and "you" in it match half of another."""
    assert selftest.content_words("Can you hear me?") == ["hear"]
    assert selftest.content_words("I can hear you loud and clear") == ["hear", "loud", "clear"]


# ----------------------------------------------------------------------- match
EXPECTED = "The purple elephant is reading the morning newspaper"  # five content words


@pytest.mark.parametrize(
    ("seen", "ok"),
    [
        ("the purple elephant is reading the morning newspaper.", True),
        ("A purple elephant reading a morning newspaper", True),  # all five, spoken a little differently
        ("purple elephant reading", True),  # 3 of 5: exactly the threshold
        ("purple elephant", False),  # 2 of 5
        ("The pink giraffe is eating the evening salad", False),
        ("", False),
    ],
)
def test_a_step_passes_on_sixty_percent_of_the_content_words(seen: str, ok: bool) -> None:
    """The threshold is inclusive at 3 of 5 and exclusive below it. Fails if it is raised (3 of 5 fails),
    lowered (2 of 5 passes), or measured on all words instead of content words."""
    assert selftest.MATCH_THRESHOLD == 0.6
    assert selftest.matches(EXPECTED, seen) is ok


def test_extra_words_do_not_lower_the_match_and_a_repeated_word_counts_once_per_expected_word() -> None:
    """The line may carry more than was asked for (another person talking over, "uh", a name). Fails if
    the ratio divides by what was seen, or a word seen once pays for two expected ones."""
    noisy = "uh the purple elephant is um reading the morning newspaper right now, hello everybody"
    assert selftest.match_ratio(EXPECTED, noisy) == 1.0
    assert selftest.match_ratio("loud loud clear", "loud") == pytest.approx(2 / 3)


def test_a_sentence_with_nothing_to_look_for_matches_nothing() -> None:
    """An expected string of only small words, or empty, is never a pass. Fails on a divide by zero or a
    vacuous 1.0 that would turn a step green without comparing anything."""
    assert selftest.match_ratio("you and me", "you and me and the whole room") == 0.0
    assert selftest.match_ratio("", "anything at all") == 0.0


def test_the_spoken_lines_the_steps_use_have_enough_to_compare() -> None:
    """Each sentence the test says, or has an agent say, has at least three content words, so 60 percent
    is a real test of it. Fails if one is edited down to a few small words."""
    for line in (
        "The purple elephant is reading the morning newspaper",
        "Claude one, can you hear me?",
        "I can hear you loud and clear",
    ):
        assert len(selftest.content_words(line)) >= 3, line


# ------------------------------------------------------------------ the secret
def test_a_secret_link_prints_as_a_placeholder_everywhere_but_reveal() -> None:
    """A traceback with locals (``pytest -l``), an f-string and a repr all call ``str`` or ``repr``. Fails if
    any of them shows the link or its passcode."""
    link = selftest.SecretLink(LINK)
    shown = [repr(link), str(link), f"{link}", f"{link!r}", repr([link]), repr({"k": link})]
    assert all(LINK not in s and PASSCODE not in s and ID not in s for s in shown), shown
    assert link.reveal() == LINK
    assert link.platform == "zoom"


def test_the_pieces_that_give_a_link_away() -> None:
    """The whole link, its query, the passcode and the meeting id, longest first. Fails if the passcode or the
    id is missing (a scrub would leave it) or a short value such as ``a=b`` is a piece (it would erase
    ordinary words from a report)."""
    parts = selftest.secret_parts(LINK + "&a=b")
    assert LINK + "&a=b" in parts and PASSCODE in parts and ID in parts and f"pwd={PASSCODE}&a=b" in parts
    assert "b" not in parts and "j" not in parts
    assert parts == sorted(parts, key=len, reverse=True)
    assert selftest.secret_parts(MEET_LINK) == [MEET_LINK, "abc-defg-hij"]


def test_scrub_removes_the_link_its_pieces_and_any_address() -> None:
    """An error from joinly or the client library can quote the link, or only a piece of it, or another
    address. Fails if any of them survives, or if text with none of them is changed."""
    secrets = selftest.secret_parts(LINK)
    quoted = f"Error calling tool 'join_meeting': {LINK} refused; code {ID}; pwd={PASSCODE}; see https://x.example/y"
    clean = selftest.scrub(quoted, secrets)
    assert ID not in clean and PASSCODE not in clean and "zoom.us" not in clean and "x.example" not in clean
    assert clean.count(selftest.REDACTED) >= 3
    assert selftest.scrub("The purple elephant, 1 of 5.", secrets) == "The purple elephant, 1 of 5."
    assert selftest.scrub("pwd=something else", []) == f"{selftest.REDACTED} else"


def test_leaks_names_the_blob_and_never_what_it_found() -> None:
    """The broker's database, its messages and its log are checked for any piece of the link. Fails if a
    blob holding a piece is not named (bytes or text), or a clean one is, or the result quotes the piece."""
    secrets = selftest.secret_parts(LINK)
    found = selftest.leaks(
        secrets,
        {
            "database": f"...{PASSCODE}...".encode(),
            "messages": "the call is live",
            "log": f"joining {ID}",
            "clean database": b"\x00\x01nothing here",
        },
    )
    assert found == ["database", "log"]
    assert all(PASSCODE not in f and ID not in f for f in found)


# ------------------------------------------------------------------- the file
def link_file(tmp_path: Path, text: str = LINK + "\n", mode: int = 0o600) -> Path:
    p = tmp_path / "link.txt"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    p.chmod(mode)
    return p


def test_a_link_file_of_mode_0600_is_read(tmp_path: Path) -> None:
    """The link comes back trimmed, as a secret that prints as a placeholder. Fails if the newline stays on
    it or the value is a bare string."""
    got = selftest.read_link_file(link_file(tmp_path))
    assert isinstance(got, selftest.SecretLink) and got.reveal() == LINK
    assert selftest.read_link_file(link_file(tmp_path, MEET_LINK)).platform == "meet"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o604, 0o666, 0o400, 0o700, 0o000])
def test_a_link_file_of_any_other_mode_is_refused_without_reading_it(tmp_path: Path, mode: int) -> None:
    """The link holds the call's passcode. Fails if a file anyone else can read is used (0644, 0640, 0604),
    or if only "readable by others" is checked and 0700 or 0400 slip through, or if the message quotes the
    file."""
    p = link_file(tmp_path, mode=mode)
    with pytest.raises(selftest.LinkFileError) as e:
        selftest.read_link_file(p)
    assert "0600" in str(e.value)
    assert LINK not in str(e.value) and PASSCODE not in str(e.value) and str(p) not in str(e.value)


def test_a_link_file_that_is_a_symlink_a_directory_or_missing_is_refused(tmp_path: Path) -> None:
    """A symlink can be swapped for another file between the check and the read, so it is not followed.
    Fails if one is followed, or a directory gives a confusing error."""
    real = link_file(tmp_path)
    link = tmp_path / "alias.txt"
    link.symlink_to(real)
    for bad in (link, tmp_path, tmp_path / "missing.txt"):
        with pytest.raises(selftest.LinkFileError):
            selftest.read_link_file(bad)


def test_a_link_file_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mode 0600 on another user's file says nothing about who can read it. Fails if the owner is not
    compared with the user running the test."""
    p = link_file(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(p).st_uid + 1)
    with pytest.raises(selftest.LinkFileError, match="owned"):
        selftest.read_link_file(p)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not a link\n",
        "http://zoom.us/j/example\n",  # not https
        "https://example.com/j/1\n",  # not Zoom, Meet or Teams
        f"{LINK}\nhttps://meet.google.com/abc-defg-hij\n",  # two lines
        "x" * 5000,
    ],
)
def test_a_link_file_without_one_meeting_link_is_refused_and_its_text_is_not_echoed(
    tmp_path: Path, text: str
) -> None:
    """The broker would refuse the link later, after two containers are up. Fails if the file is accepted
    as it is, or the refusal quotes what was in it."""
    with pytest.raises(selftest.LinkFileError) as e:
        selftest.read_link_file(link_file(tmp_path, text))
    assert text.strip() == "" or text.strip()[:20] not in str(e.value)


# ------------------------------------------------------- what joinly and the broker send
def test_batch_items_reads_a_real_rendered_batch() -> None:
    """The agent's batch is the broker's own ``render_batch``. Fails if the id, sender, kind or the
    "addressed to you" flag are read wrongly, if the quoted text is not unquoted (a comma or a quote in a
    call line), or if the header and footer lines are taken for messages."""
    call = item(7, 1, kind="meeting", text='Claude one, can you "hear" me?')
    other = item(8, 0, kind="meeting", text="just talking")
    batch = envelope.render_batch(
        [call, other],
        room="#build",
        recipient="claude-1",
        human_name="alice",
        token="yk:b7.0123abcd",
        peer_inline=True,
    )
    got = selftest.batch_items(batch)
    assert [(i.id, i.kind, i.to_you) for i in got] == [(7, "meeting", True), (8, "meeting", False)]
    assert got[0].text == 'Claude one, can you "hear" me?' and got[1].text == "just talking"
    assert selftest.batch_items("[switchboard] #build: no new messages.") == []


def result(structured: Any = None, text: str | None = None) -> Any:
    block = SimpleNamespace(text=text) if text is not None else None
    return SimpleNamespace(structured_content=structured, content=[block] if block else [])


SEGMENTS = {
    "segments": [
        {"text": "hello", "start": 1.0, "end": 2.0, "speaker": "Alice", "role": "participant"},
        {"text": "said by the bot itself", "start": 3.0, "end": 4.5, "speaker": None, "role": "assistant"},
        {"text": "I can hear you", "start": 6.0, "end": 7.25, "speaker": " ", "role": "participant"},
        {"text": "bad", "start": "x", "end": 1},
        "not a segment",
    ]
}


@pytest.mark.parametrize("shape", ["structured", "text", "wrapped"])
def test_segments_are_read_from_whichever_shape_the_client_gives(shape: str) -> None:
    """``get_transcript`` returns the segments as structured content, as JSON text, or inside a ``result``
    key, depending on the client library. Fails if one shape reads as empty, or a malformed segment is
    kept (it would crash the step instead of failing it)."""
    res = {
        "structured": result(structured=SEGMENTS),
        "text": result(text=json.dumps(SEGMENTS)),
        "wrapped": result(structured={"result": SEGMENTS}),
    }[shape]
    got = selftest.segments_of(res)
    assert [s.text for s in got] == ["hello", "said by the bot itself", "I can hear you"]
    assert got[2].speaker is None and got[1].role == "assistant"
    assert selftest.segments_of(result()) == [] and selftest.segments_of(result(text="not json")) == []


async def test_the_parsers_read_what_the_client_library_returns_for_joinlys_tools() -> None:
    """The three tools the test reads, served in the shapes joinly v0.5.4's own server returns them (recorded
    by running its tools over a stub session, 2026-11-04): ``get_transcript`` an object with ``segments``,
    ``get_participants`` a list that the server wraps in ``result``, ``speak_text`` a bare string. The
    results are the real client's objects, not a stand-in. Fails if the attribute the parser reads is not
    the one the client library has, or a wrapped list reads as nobody."""
    from fastmcp import Client, FastMCP

    server = FastMCP("joinly-shapes")

    @server.tool
    def get_transcript(mode: str = "full") -> dict[str, Any]:
        return SEGMENTS

    @server.tool
    def get_participants() -> list[dict[str, Any]]:
        return [
            {"name": "switchboard (transcribing)", "email": None, "infos": []},
            {"name": "switchboard self-test speaker (Me)", "email": None, "infos": ["Host"]},
        ]

    @server.tool
    def speak_text(text: str) -> str:
        return "Finished speaking."

    async with Client(server) as client:
        segs = selftest.segments_of(await client.call_tool("get_transcript", {"mode": "full"}))
        names = selftest.names_of(await client.call_tool("get_participants", {}))
    assert [s.text for s in segs] == ["hello", "said by the bot itself", "I can hear you"]
    assert names == ["switchboard (transcribing)", "switchboard self-test speaker (Me)"]
    assert selftest.has_name(names, "transcribing")


def test_what_was_heard_is_what_others_said_after_the_step_began() -> None:
    """Step 4 compares what the speaker heard from the other bot, after the point where it began. Fails if
    the speaker's own speech (role ``assistant``) counts as heard, or if words from before the step do."""
    segs = selftest.segments_of(result(structured=SEGMENTS))
    assert selftest.last_end(segs) == 7.25 and selftest.last_end([]) == 0.0
    assert selftest.heard_since(segs, 0.0) == "hello I can hear you"
    assert selftest.heard_since(segs, 2.0) == "I can hear you"
    assert selftest.heard_since(segs, 7.25) == ""


def test_participant_names_are_read_and_the_bot_is_found_by_a_word() -> None:
    """``get_participants`` returns a list of ``{name, ...}``, bare or under ``result``. The bot is found
    by the word "transcribing", so Zoom's own additions to a name do not hide it. Fails if a shape reads as
    nobody, or the bot is missed with a suffix or found in a name that merely starts alike."""
    plain = [
        {"name": "switchboard (transcribing)"},
        {"name": "switchboard self-test speaker (Me)", "infos": []},
    ]
    for res in (
        result(structured=plain),
        result(structured={"result": plain}),
        result(text=json.dumps(plain)),
    ):
        names = selftest.names_of(res)
        assert names == ["switchboard (transcribing)", "switchboard self-test speaker (Me)"]
        assert selftest.has_name(names, "transcribing")
        assert not selftest.has_name(names[1:], "transcribing")
    assert selftest.has_name(["Switchboard (Transcribing) (Guest)"], "transcribing")
    assert not selftest.has_name(["Transcribingly Yours"], "transcribing")
    assert selftest.names_of(result()) == []


# ------------------------------------------------------------------------ report
def steps_with(seen: str = "ok") -> list[selftest.StepResult]:
    return [
        selftest.StepResult(1, "the bar goes live", True, "live", seen, 12.34),
        selftest.StepResult(
            2, "a spoken line | reaches\nthe room", False, "the purple elephant", "purple", 31.0, 0.4
        ),
        selftest.StepResult.not_attempted(3, "a name wakes an agent", "claude-1 woken", "step 2 failed"),
    ]


def test_the_report_has_the_steps_their_strings_and_their_timings(tmp_path: Path) -> None:
    """The JSON is for a program and the Markdown for a person; both hold every step's expected and seen
    strings, its seconds, and a verdict. Fails if a step is dropped, a failed step is called a pass, a
    step that was not attempted is not marked, or a pipe or newline breaks the table."""
    json_path, md_path = selftest.write_report(tmp_path / "out", steps_with(), [], {"platform": "zoom"})
    data = json.loads(json_path.read_text())
    assert data["result"] == "fail" and data["threshold"] == 0.6 and data["platform"] == "zoom"
    assert [s["n"] for s in data["steps"]] == [1, 2, 3]
    assert data["steps"][1]["expected"] == "the purple elephant" and data["steps"][1]["seen"] == "purple"
    assert data["steps"][0]["seconds"] == pytest.approx(12.34) and data["steps"][1]["ratio"] == 0.4
    assert data["steps"][2]["attempted"] is False and data["steps"][2]["seen"].startswith("not attempted")
    md = md_path.read_text()
    rows = [line for line in md.splitlines() if line.startswith("| ") and not line.startswith("| #")]
    assert len(rows) == 3 and all(r.count("|") - r.count("\\|") == 8 for r in rows), rows
    assert "**FAIL**: 1 of 3 steps passed" in md and "FAIL (not attempted)" in md and "40%" in md


def test_the_report_is_private_whatever_the_umask(tmp_path: Path) -> None:
    """The report has what was said in the call. Fails if the folder or a file is readable by others under
    a 022 umask, or if a file that already existed keeps its old, wider mode."""
    old = os.umask(0o022)
    try:
        out = tmp_path / "out"
        out.mkdir(mode=0o755)
        (out / selftest.REPORT_MD).write_text("old")
        (out / selftest.REPORT_MD).chmod(0o644)
        fresh = tmp_path / "fresh"
        paths = selftest.write_report(fresh, steps_with(), []) + selftest.write_report(out, steps_with(), [])
    finally:
        os.umask(old)
    assert oct(fresh.stat().st_mode & 0o777) == oct(0o700)
    assert [oct(p.stat().st_mode & 0o777) for p in paths] == [oct(0o600)] * 4


def test_an_all_passing_report_says_pass_and_an_empty_one_does_not(tmp_path: Path) -> None:
    """Fails if a run that recorded no step, or one with a problem beside its green steps, reads as a pass."""
    ok = [selftest.StepResult(1, "a", True, "x", "x", 1.0)]
    assert json.loads(selftest.write_report(tmp_path / "a", ok, [])[0].read_text())["result"] == "pass"
    assert json.loads(selftest.write_report(tmp_path / "b", [], [])[0].read_text())["result"] == "fail"
    # six green steps and a container left behind is not a pass
    both = selftest.write_report(tmp_path / "c", ok, [], {"problems": ["a container is still there"]})
    assert json.loads(both[0].read_text())["result"] == "fail" and "**FAIL**" in both[1].read_text()
    assert (
        "but there are problems" in both[1].read_text()
        and "a container is still there" in both[1].read_text()
    )
    assert selftest.failed_steps(steps_with()) == [
        "2 (a spoken line | reaches\nthe room)",
        "3 (a name wakes an agent)",
    ]


def test_the_report_never_holds_the_link_or_a_piece_of_it(tmp_path: Path) -> None:
    """An error quoted into a step's text, or a note, must not put the link, the passcode or the meeting id in
    either file. Fails if a string is written unscrubbed, in the steps or the extra fields."""
    secrets = selftest.secret_parts(LINK)
    steps = steps_with(seen=f"Error calling tool: {LINK} ({ID}, {PASSCODE})")
    meta = {"platform": "zoom", "problems": [f"container for {ID} stayed"], "cleanup": {"note": PASSCODE}}
    for path in selftest.write_report(tmp_path, steps, secrets, meta):
        text = path.read_text()
        assert LINK not in text and ID not in text and PASSCODE not in text and "zoom.us" not in text, (
            path.name
        )
        assert selftest.REDACTED in text


def test_a_report_that_still_holds_a_piece_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scrub is not trusted alone. If a piece survives it (here, a scrub that does nothing), nothing is
    written and the error does not say what was found. Fails if the files are written anyway."""
    monkeypatch.setattr(selftest, "scrub", lambda text, secrets: text)
    with pytest.raises(selftest.ReportLeak) as e:
        selftest.write_report(tmp_path / "out", steps_with(seen=PASSCODE), selftest.secret_parts(LINK))
    assert PASSCODE not in str(e.value) and not (tmp_path / "out").exists()


def test_a_long_string_is_cut_in_the_table_but_whole_in_the_json(tmp_path: Path) -> None:
    """Fails if the table grows past a screen, or the JSON loses the full string."""
    long = "word " * 200
    json_path, md_path = selftest.write_report(
        tmp_path, [selftest.StepResult(1, "a", True, long, long, 1.0)], []
    )
    assert json.loads(json_path.read_text())["steps"][0]["seen"] == long
    assert "…" in md_path.read_text() and long.strip() not in md_path.read_text()


def test_a_pinned_file_runs_only_with_its_own_bytes() -> None:
    """The Zoom fix the self-test mounts into joinly is fetched, then checked against its pinned sha256:
    a changed or truncated download is refused, not mounted. Fails if the check is skipped or compares
    the wrong thing."""
    import hashlib

    import pytest

    data = b"def patched(): pass\n"
    good = hashlib.sha256(data).hexdigest()
    assert selftest.check_pinned(data, good) == data
    assert selftest.check_pinned(data, good.upper()) == data
    with pytest.raises(selftest.PinnedFileError):
        selftest.check_pinned(data + b"#", good)
    with pytest.raises(selftest.PinnedFileError):
        selftest.check_pinned(b"", good)

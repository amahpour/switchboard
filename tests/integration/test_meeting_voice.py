"""Agents speak in a call, and a room sees its shared screen (#593, DESIGN.md §52).

The broker is the real one and joinly is ``FakeJoinly``, which records what it is asked to say
and hands back a small picture. Voice is a person's choice per call, off when a call starts. A
snapshot is a room file (§53) posted as a card. Nothing waits on a sleep: a test waits for the
event it expects, and the ten-second limit is an advance of the broker's ``FakeClock``.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly
from test_meetings import SECRET, ZOOM, command, history, meeting_lines, notices, seg, wait_until

from switchboard.config import Config, FilesCfg, MeetingCfg
from switchboard.meeting.speech import VOICE_HINT

MEET = "https://meet.google.com/abc-defg-hij"
DOWNLOAD_RE = re.compile(r"download: (http://\S+/api/files/([0-9a-f]{32})\?t=[A-Za-z0-9_-]+)")


def make_broker(
    home: Path, joinly: FakeJoinly, *, clock: FakeClock | None = None, voice: str | None = None, mb: int = 25
) -> InProcBroker:
    meeting = MeetingCfg(joinly_url=joinly.url, **({"tts_voice": voice} if voice else {}))
    cfg = Config(human_name="alice", files=FilesCfg(max_mb=mb)).replace(meeting=meeting)
    cfg = cfg.with_delivery(quiet_s=0.0, max_hold_s=0.0, unaddressed_hold_s=0.0, rate_limit_s=0.0)
    b = InProcBroker(home, cfg, clock=clock).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web  # type: ignore[attr-defined]
    return b


@pytest.fixture
def joinly() -> Iterator[FakeJoinly]:
    server = FakeJoinly().start()
    yield server
    server.stop()


@pytest.fixture
def broker(tmp_home: Path, joinly: FakeJoinly) -> Iterator[InProcBroker]:
    b = make_broker(tmp_home, joinly)
    yield b
    b.web.close()  # type: ignore[attr-defined]
    b.stop()


@pytest.fixture
def timed(tmp_home: Path, joinly: FakeJoinly) -> Iterator[tuple[InProcBroker, FakeClock]]:
    clock = FakeClock()
    b = make_broker(tmp_home, joinly, clock=clock)
    yield b, clock
    b.web.close()  # type: ignore[attr-defined]
    b.stop()


def call_state(b: InProcBroker) -> dict[str, Any] | None:
    """The room frame's ``settings.meeting``, as the page reads it."""
    r = b.web.get("/api/rooms/build/members", headers=b.write_headers())  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    return r.json()["settings"]["meeting"]


async def start_call(b: InProcBroker, joinly: FakeJoinly, link: str = MEET) -> None:
    joined = len(joinly.joins)
    command(b, f"/meeting {link}")
    await wait_until(lambda: len(joinly.joins) == joined + 1)
    await wait_until(lambda: (call_state(b) or {}).get("state") == "live")


def refused(b: InProcBroker, text: str) -> Any:
    return b.web.post(  # type: ignore[attr-defined]
        "/api/rooms/build/command", json={"text": text}, headers=b.write_headers()
    )


def files(b: InProcBroker) -> list[tuple[str, str, str, str]]:
    con = sqlite3.connect(b.paths.db)
    rows = con.execute(
        "SELECT name, uploader_kind, uploader_name, state FROM files ORDER BY created_at"
    ).fetchall()
    con.close()
    return [tuple(r) for r in rows]


def count(items: list[str], needle: str) -> int:
    return sum(1 for i in items if needle in i)


def notice_texts(b: InProcBroker) -> list[str]:
    return [m["text"] for m in history(b) if m["kind"] == "notice"]


# ---------------------------------------------------------------- the switch


async def test_voice_is_off_at_the_start_and_only_a_person_turns_it_on_and_off(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """The page frame says ``voice: false`` when a call starts. A person's ``/meeting voice on`` and
    ``off`` change it, and each change leaves a notice; saying it again changes nothing and leaves
    none. An agent's ``/meeting voice on`` is only text. Fails if voice starts on, an agent can
    turn it on, or a change is silent."""
    assert "no call" in command(broker, "/meeting voice on")["text"]
    await start_call(broker, joinly)
    assert call_state(broker) == {"state": "live", "platform": "meet", "voice": False}

    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        said = await agent.say("#build", "/meeting voice on")
        assert said.get("posted_id")
        await agent.say("#build", "/meeting snapshot")
    assert call_state(broker)["voice"] is False  # type: ignore[index]
    assert joinly.snapshots == 0

    assert "voice is on" in command(broker, "/meeting voice on")["text"]
    assert call_state(broker)["voice"] is True  # type: ignore[index]
    assert "already on" in command(broker, "/meeting voice on")["text"]
    assert "voice is off" in command(broker, "/meeting voice off")["text"]
    assert call_state(broker)["voice"] is False  # type: ignore[index]
    assert "already off" in command(broker, "/meeting voice off")["text"]
    seen = notice_texts(broker)
    assert count(seen, "alice turned voice on") == 1
    assert count(seen, "alice turned voice off") == 1
    assert "starts with @call" in "\n".join(seen)


async def test_voice_is_off_again_in_the_next_call(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """Voice belongs to one call. Fails if leaving and starting again keeps it on."""
    await start_call(broker, joinly)
    command(broker, "/meeting voice on")
    command(broker, "/meeting leave")
    await wait_until(lambda: call_state(broker) is None)
    await start_call(broker, joinly)
    assert call_state(broker)["voice"] is False  # type: ignore[index]


@pytest.mark.parametrize("text", ["/meeting voice", "/meeting voice loud", "/meeting voice on now"])
async def test_a_malformed_voice_command_is_refused(broker: InProcBroker, text: str) -> None:
    r = refused(broker, text)
    assert r.status_code == 400 and "usage" in r.text


# --------------------------------------------------------------- speaking


async def test_an_at_call_line_is_spoken_only_while_voice_is_on(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """With voice on, what follows ``@call`` is spoken, made speakable, and the room still gets the
    message as written. With voice off, or a message that does not start with ``@call``, it is
    only text. The last line proves the earlier ones were never queued: it is the only one spoken.
    Fails if voice-off lines are spoken, a later mention starts speech, or the room loses the line."""
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await start_call(broker, joinly)
        await agent.say("#build", "@call this is said while voice is off")
        command(broker, "/meeting voice on")
        await agent.say("#build", "tell @call something, not at the start")
        await agent.say("#build", "@calls are not @call")
        said = await agent.say("#build", "@call **All** green, see [the run](https://example.com/run/1).")
        assert said.get("posted_id")
        await wait_until(lambda: len(joinly.spoken) == 1)
        command(broker, "/meeting voice off")
        await agent.say("#build", "@call this is said after voice went off")
        command(broker, "/meeting voice on")
        await agent.say("#build", "@call the last one")
        await wait_until(lambda: len(joinly.spoken) == 2)
    assert joinly.spoken == ["All green, see the run.", "the last one"]
    texts = [m["text"] for m in history(broker)]
    assert "@call **All** green, see [the run](https://example.com/run/1)." in texts
    assert "@call this is said while voice is off" in texts


async def test_voice_does_nothing_without_a_call(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """No call, no speech: an ``@call`` line is a plain message and no refusal is posted. Fails
    if a line is queued with nobody to hear it, or the agent is told off for it."""
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        said = await agent.say("#build", "@call anyone there?")
        assert said.get("posted_id") and "note" not in said
    assert joinly.spoken == []
    assert notices(broker) == "" or "voice" not in notices(broker).lower()


async def test_speaking_never_holds_up_a_say_and_a_full_queue_drops_with_one_notice(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """``speak_text`` returns only when the speech is over. While joinly is mid-line the agent's
    says still return at once; three more lines wait and the fifth and sixth are dropped, with one
    notice. Then the waiting lines are spoken in order. Fails if a say waits on speech, the queue
    grows without a bound, or each dropped line adds a notice."""
    joinly.hold_speech = True
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await start_call(broker, joinly)
        command(broker, "/meeting voice on")
        first = await agent.say("#build", "@call line one")
        assert first.get("posted_id")
        await wait_until(lambda: joinly.spoken == ["line one"])  # joinly is now holding it
        for text in ("two", "three", "four", "five", "six"):
            assert (await agent.say("#build", f"@call line {text}")).get("posted_id")
        await wait_until(lambda: count(notice_texts(broker), "waiting to be spoken") == 1)
        joinly.release_speech()
        await wait_until(lambda: len(joinly.spoken) == 4)
        await agent.say("#build", "@call after the burst")
        await wait_until(lambda: len(joinly.spoken) == 5)
    assert joinly.spoken == ["line one", "line two", "line three", "line four", "after the burst"]
    assert count(notice_texts(broker), "waiting to be spoken") == 1


async def test_turning_voice_off_drops_what_was_waiting(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """Lines already queued are not spoken once a person turns voice off. Fails if the queue is
    left to drain into a call the person has just silenced."""
    joinly.hold_speech = True
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await start_call(broker, joinly)
        command(broker, "/meeting voice on")
        await agent.say("#build", "@call spoken")
        await wait_until(lambda: joinly.spoken == ["spoken"])
        await agent.say("#build", "@call queued one")
        await agent.say("#build", "@call queued two")
        command(broker, "/meeting voice off")
        joinly.release_speech()
        command(broker, "/meeting voice on")
        await agent.say("#build", "@call sentinel")
        await wait_until(lambda: len(joinly.spoken) == 2)
    assert joinly.spoken == ["spoken", "sentinel"]


async def test_an_interrupted_line_is_noticed(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """joinly answers ``speak_text`` with a message when someone talks over the bot. The room
    says so. Fails if the reply is dropped on the floor."""
    joinly.interrupt_next = True
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await start_call(broker, joinly)
        command(broker, "/meeting voice on")
        await agent.say("#build", "@call heading to the shop")
        await wait_until(lambda: any("spoke over" in t for t in notice_texts(broker)))


async def test_a_line_joinly_cannot_speak_is_noticed(broker: InProcBroker, joinly: FakeJoinly) -> None:
    joinly.fail_speech = True
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await start_call(broker, joinly)
        command(broker, "/meeting voice on")
        await agent.say("#build", "@call hello")
        await wait_until(lambda: any("Couldn't speak" in t for t in notice_texts(broker)))


async def test_call_is_a_reserved_name_so_an_agent_cannot_be_the_call(broker: InProcBroker) -> None:
    """An agent named ``call`` would make ``@call`` both a mention and a spoken line. Fails if the
    join is accepted."""
    async with FakeAgent(broker.home, "k1") as agent:
        got = await agent.join("#build", "call")
        assert got["ok"] is False and got["code"] == "name_reserved"


# ------------------------------------------------- the voice, and the wake line


@pytest.mark.parametrize(("voice", "expected"), [(None, "bm_george"), ("af_bella", "af_bella")])
async def test_the_voice_goes_to_joinly_in_its_settings(
    tmp_home: Path, joinly: FakeJoinly, voice: str | None, expected: str
) -> None:
    """``[meeting] tts_voice`` reaches joinly in the ``joinly-settings`` header as ``tts_args.voice``,
    with the earlier keys intact. Fails if the voice is left out, or replaces the hotwords."""
    b = make_broker(tmp_home, joinly, voice=voice)
    try:
        await start_call(b, joinly)
        sent = [json.loads(s) for s in joinly.settings]
        assert sent and all(s.get("tts_args") == {"voice": expected} for s in sent), sent
        assert all(s.get("name") == "switchboard (transcribing)" and "stt_args" in s for s in sent)
    finally:
        b.web.close()  # type: ignore[attr-defined]
        b.stop()


async def test_a_wake_with_call_lines_says_how_to_speak_only_while_voice_is_on(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """A batch that carries call lines adds one line saying how to speak, while voice is on. A
    batch without call lines, or with voice off, does not. Fails if the line is always there, never
    there, or added to a person's message."""
    async with FakeAgent(broker.home, "k1") as claude:
        await claude.join("#build", "claude-1")
        await start_call(broker, joinly)
        named = claude.wait_task("#build", timeout_s=30)
        joinly.push([seg("Claude one, are you there", 0.0, 2.0)])
        off = await named
        assert "kind=meeting" in off["text"] and VOICE_HINT not in off["text"]

        command(broker, "/meeting voice on")
        named = claude.wait_task("#build", timeout_s=30)
        joinly.push([seg("Claude one, are you still there", 20.0, 22.0)])
        on = await named
        assert "kind=meeting" in on["text"] and on["text"].count(VOICE_HINT) == 1
        assert len(VOICE_HINT) < 90

        heard = claude.wait_task("#build", timeout_s=30)
        broker.web.post(  # type: ignore[attr-defined]
            "/api/rooms/build/say", json={"text": "@claude-1 and from me"}, headers=broker.write_headers()
        )
        person = await heard
        assert "from me" in person["text"] and VOICE_HINT not in person["text"]


# ---------------------------------------------------------------- snapshot


def cards(b: InProcBroker) -> list[dict[str, Any]]:
    return [m for m in history(b) if "switchboard:file/" in m["text"]]


async def test_snapshot_by_command_is_a_room_file_a_card_and_a_download_line(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """``/meeting snapshot`` stores joinly's frame as the person's room file and posts the card as
    their message. It downloads as an attachment, byte for byte. The agent it wakes is handed a
    short-lived ``download:`` line. Fails if the frame is not stored, the card is missing or comes
    from the wrong sender, or the agent gets no link."""
    async with FakeAgent(broker.home, "k1") as claude:
        await claude.join("#build", "claude-1")
        await start_call(broker, joinly)
        waiting = claude.wait_task("#build", timeout_s=30)
        assert "snapshot" in command(broker, "/meeting snapshot")["text"]
        got = await waiting
    await wait_until(lambda: len(cards(broker)) == 1)
    card = cards(broker)[0]
    assert card["sender_kind"] == "human" and card["from"] == "alice"
    assert re.fullmatch(
        r"artifact: screen-\d{6}\.png \| \d+ \| sha256:[0-9a-f]{64} \| switchboard:file/[0-9a-f]{32}",
        card["text"],
    )
    assert [f["name"] for f in card["files"]] == [files(broker)[0][0]]
    assert files(broker) == [(card["files"][0]["name"], "human", "alice", "stored")]
    assert joinly.snapshots == 1
    link = DOWNLOAD_RE.search(got["text"])
    assert link is not None, got["text"]
    r = broker.web.get(f"/api/files/{card['files'][0]['id']}", headers=broker.write_headers())  # type: ignore[attr-defined]
    assert r.status_code == 200 and r.content == joinly.frame
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["content-type"] == "application/octet-stream"
    assert (broker.paths.files_dir / card["files"][0]["id"]).is_file()  # an ordinary room file (§53)


async def test_a_call_out_that_says_screen_posts_the_picture_first(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """Saying "Claude one, look at my screen" takes a snapshot before the line is posted, as a message from
    the call. The woken agent's one batch holds the picture, then the line, with a download link.
    A call-out without the word takes none. Fails if the picture comes after the line, is from a
    person, or a call-out without "screen" takes one."""
    async with FakeAgent(broker.home, "k1") as claude:
        await claude.join("#build", "claude-1")
        await start_call(broker, joinly)
        named = claude.wait_task("#build", timeout_s=30)
        joinly.push([seg("Claude one, run the tests", 0.0, 2.0)])
        plain = await named
        assert "run the tests" in plain["text"] and joinly.snapshots == 0 and cards(broker) == []

        named = claude.wait_task("#build", timeout_s=30)
        joinly.push([seg("Claude one, look at my screen", 20.0, 22.0)])
        got = await named
    assert joinly.snapshots == 1
    card = cards(broker)[0]
    assert card["sender_kind"] == "meeting" and card["from"] == "call"
    line = next(m for m in meeting_lines(broker) if "look at my screen" in m["text"])
    assert card["id"] < line["id"]
    text = got["text"]  # one batch: the line that woke the agent, and the picture that came before it
    assert "look at my screen" in text and "artifact: screen-" in text
    assert DOWNLOAD_RE.search(text) is not None, text
    assert files(broker)[0][1:] == ("meeting", "call", "stored")


async def test_a_second_snapshot_inside_ten_seconds_is_refused(
    timed: tuple[InProcBroker, FakeClock], joinly: FakeJoinly
) -> None:
    """One snapshot every ten seconds per call, whichever way it is asked for. The clock is the
    broker's: an advance, not a sleep. A call-out inside the window still posts its line, with a
    notice and no picture. Fails if a second request goes through, the window is longer or shorter,
    or the line is held back."""
    b, clock = timed
    async with FakeAgent(b.home, "k1") as claude:
        await claude.join("#build", "claude-1")
        await start_call(b, joinly)
        command(b, "/meeting snapshot")
        await wait_until(lambda: joinly.snapshots == 1 and len(cards(b)) == 1)
        await claude.read("#build")  # the person's card is read, so the next wake is the call-out

        r = refused(b, "/meeting snapshot")
        assert r.status_code == 409 and "less than 10 seconds" in r.text
        clock.advance(9.5)
        assert refused(b, "/meeting snapshot").status_code == 409
        assert joinly.snapshots == 1

        named = claude.wait_task("#build", timeout_s=30)
        joinly.push([seg("Claude one, look at my screen", 0.0, 2.0)])
        got = await named
        assert "look at my screen" in got["text"]
        assert joinly.snapshots == 1 and len(cards(b)) == 1
        assert any("less than 10 seconds" in t for t in notice_texts(b))

        clock.advance(1.0)
        command(b, "/meeting snapshot")
        await wait_until(lambda: joinly.snapshots == 2 and len(cards(b)) == 2)


async def test_a_snapshot_needs_the_bot_in_the_call(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """No call, or a call still joining, has nothing to photograph. Fails if the command pretends."""
    assert "no call" in command(broker, "/meeting snapshot")["text"]
    assert joinly.snapshots == 0


@pytest.mark.parametrize(
    ("frame", "claimed", "why"),
    [
        (b"<html><script>alert(1)</script></html>", "image/png", "not a PNG, JPEG or WebP"),
        (b"GIF89a" + b"\x00" * 64, "image/gif", "not a PNG, JPEG or WebP"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml", "not a PNG, JPEG or WebP"),
    ],
)
async def test_something_that_is_not_a_picture_is_refused_whatever_it_claims(
    broker: InProcBroker, joinly: FakeJoinly, frame: bytes, claimed: str, why: str
) -> None:
    """The bytes decide, not the type joinly names. Fails if HTML, a GIF or an SVG becomes a file, even
    one called image/png."""
    joinly.frame, joinly.frame_type = frame, claimed
    await start_call(broker, joinly)
    command(broker, "/meeting snapshot")
    await wait_until(lambda: any(why in t for t in notice_texts(broker)))
    assert files(broker) == [] and cards(broker) == []


async def test_a_real_picture_is_kept_whatever_type_joinly_names(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """The other direction: a PNG called text/plain is still a PNG, and is named by what it is."""
    joinly.frame_type = "text/plain"
    await start_call(broker, joinly)
    command(broker, "/meeting snapshot")
    await wait_until(lambda: len(cards(broker)) == 1)
    assert files(broker)[0][0].endswith(".png")


async def test_a_failed_snapshot_is_noticed_and_the_call_out_line_is_still_posted(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """joinly erroring does not hold the line back, and the room is told. Fails if the line is lost
    or the failure is silent."""
    joinly.fail_snapshot = True
    async with FakeAgent(broker.home, "k1") as claude:
        await claude.join("#build", "claude-1")
        await start_call(broker, joinly)
        named = claude.wait_task("#build", timeout_s=30)
        joinly.push([seg("Claude one, look at my screen", 0.0, 2.0)])
        got = await named
    assert "look at my screen" in got["text"] and "artifact:" not in got["text"]
    assert any("Couldn't get a snapshot" in t for t in notice_texts(broker))
    assert files(broker) == []


# ----------------------------------------------------------- the passcode


async def test_the_passcode_is_still_nowhere_after_speaking_and_snapshots(
    broker: InProcBroker, joinly: FakeJoinly, caplog: pytest.LogCaptureFixture
) -> None:
    """Voice and snapshots go through the same session as the join. The link's passcode is in the
    join and nowhere in the database, the notices, the card or the log. Fails if a new path logs
    the arguments of a call, or keeps the link."""
    caplog.set_level(logging.DEBUG)
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await start_call(broker, joinly, ZOOM)
        assert joinly.joins[-1]["meeting_url"] == ZOOM
        command(broker, "/meeting voice on")
        await agent.say("#build", "@call hello")
        await wait_until(lambda: joinly.spoken == ["hello"])
        command(broker, "/meeting snapshot")
        await wait_until(lambda: len(cards(broker)) == 1)
        command(broker, "/meeting leave")
        await wait_until(lambda: call_state(broker) is None)
    blob = Path(broker.paths.db).read_bytes()
    wal = Path(str(broker.paths.db) + "-wal")
    if wal.exists():
        blob += wal.read_bytes()
    assert SECRET.encode() not in blob
    assert SECRET not in notices(broker)
    assert SECRET not in caplog.text
    assert SECRET not in "\n".join(m["text"] for m in history(broker))

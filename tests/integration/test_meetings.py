"""A room transcribes a call through a scripted joinly (DESIGN.md §52).

The broker is the real one. joinly is ``FakeJoinly``: it records the join, the hotwords
and the leave, and pushes transcript segments. A passcode in the link is how the
call is joined and is never written down.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly

from switchboard.config import Config, MeetingCfg

SECRET = "SECRET99"
ZOOM = f"https://us02web.zoom.us/j/123456789?pwd={SECRET}"


async def wait_until(cond, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        await __import__("asyncio").sleep(0.05)


@pytest.fixture
def joinly() -> Iterator[FakeJoinly]:
    server = FakeJoinly().start()
    yield server
    server.stop()


def _broker(
    tmp_home: Path, joinly: FakeJoinly, max_calls: int, stt_model: str = ""
) -> Iterator[InProcBroker]:
    meeting = MeetingCfg(joinly_url=joinly.url, max_calls=max_calls, stt_model=stt_model)
    cfg = Config(human_name="alice").replace(meeting=meeting)
    cfg = cfg.with_delivery(quiet_s=0.0, max_hold_s=0.0, unaddressed_hold_s=0.0)
    b = InProcBroker(tmp_home, cfg).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web  # type: ignore[attr-defined]
    yield b
    web.close()
    b.stop()


@pytest.fixture
def broker(tmp_home: Path, joinly: FakeJoinly) -> Iterator[InProcBroker]:
    """A broker that follows one call at a time, as a default install does."""
    yield from _broker(tmp_home, joinly, 1)


@pytest.fixture
def broker_two_calls(tmp_home: Path, joinly: FakeJoinly) -> Iterator[InProcBroker]:
    """The same, with ``[meeting] max_calls = 2``."""
    yield from _broker(tmp_home, joinly, 2)


@pytest.fixture
def broker_with_model(
    request: pytest.FixtureRequest, tmp_home: Path, joinly: FakeJoinly
) -> Iterator[InProcBroker]:
    """A broker with ``[meeting] stt_model`` set to the test's parameter (an empty string is unset)."""
    yield from _broker(tmp_home, joinly, 1, request.param)


def command(b: InProcBroker, text: str, room: str = "build") -> dict:
    r = b.web.post(f"/api/rooms/{room}/command", json={"text": text}, headers=b.write_headers())  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    return r.json()


def history(b: InProcBroker) -> list[dict]:
    r = b.web.get("/api/rooms/build/messages", headers=b.write_headers())  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    return r.json()["messages"]


def notices(b: InProcBroker) -> str:
    return "\n".join(m["text"] for m in history(b) if m["kind"] == "notice")


def meeting_lines(b: InProcBroker) -> list[dict]:
    return [m for m in history(b) if m.get("sender_kind") == "meeting"]


def open_calls(b: InProcBroker) -> int:
    con = sqlite3.connect(b.paths.db)
    n = con.execute("SELECT COUNT(*) FROM meetings WHERE state!='ended'").fetchone()[0]
    con.close()
    return int(n)


def call_states(b: InProcBroker) -> list[str]:
    """The state of every call that is not over, in the order they began."""
    con = sqlite3.connect(b.paths.db)
    rows = con.execute("SELECT state FROM meetings WHERE state!='ended' ORDER BY id").fetchall()
    con.close()
    return [r[0] for r in rows]


def meeting_rows(b: InProcBroker) -> int:
    """Every call ever started, over or not: a refused start must not add one."""
    con = sqlite3.connect(b.paths.db)
    n = con.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    con.close()
    return int(n)


def make_room(b: InProcBroker, name: str) -> str:
    r = b.web.post("/api/rooms", json={"name": f"#{name}"}, headers=b.write_headers())  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    return name


def try_meeting(b: InProcBroker, link: str, room: str):
    """``/meeting <link>`` in ``room``, whatever the answer is."""
    return b.web.post(  # type: ignore[attr-defined]
        f"/api/rooms/{room}/command", json={"text": f"/meeting {link}"}, headers=b.write_headers()
    )


def seg(text: str, start: float, end: float, speaker: str | None = None, role: str = "participant") -> dict:
    return {"text": text, "start": start, "end": end, "speaker": speaker, "role": role}


@pytest.mark.parametrize(
    ("broker_with_model", "expected"),
    [("", None), ("base", "base"), ("large-v3-turbo", "large-v3-turbo")],
    indirect=["broker_with_model"],
    ids=["unset", "base", "turbo"],
)
async def test_the_stt_model_reaches_joinly_as_stt_args_model_name_and_unset_leaves_it_out(
    broker_with_model: InProcBroker, joinly: FakeJoinly, expected: str | None
) -> None:
    """``[meeting] stt_model`` (#617) goes to joinly in the ``joinly-settings`` header as
    ``stt_args.model_name``, next to the hotwords, on every connection of the call. Unset, the key is
    absent altogether, and not an empty string: joinly turns on its Hugging Face lookups for any
    ``model_name`` that is not ``None`` (v0.5.4's whisper.py: ``local_files_only`` is off once the
    argument is given), so even an empty one would make a default install reach out to the network.
    Fails if the model is left out when set, sent when it is not, sent as an empty string, or sent in
    place of the hotwords."""
    b = broker_with_model
    async with FakeAgent(b.home, "k1") as claude:
        assert (await claude.join("#build", "claude-1"))["ok"]
        command(b, f"/meeting {ZOOM}")
        await wait_until(lambda: len(joinly.joins) == 1)
    sent = [json.loads(s) for s in joinly.settings]
    assert sent, "joinly was never sent a header"
    for header in sent:
        stt = header["stt_args"]
        assert "claude one" in [w.lower() for w in stt["hotwords"]], "the hotwords stay"
        if expected is None:
            assert "model_name" not in stt, stt
        else:
            assert stt["model_name"] == expected, stt
        assert set(stt) <= {"hotwords", "model_name"}, "nothing else rides along in stt_args"


async def test_a_link_joins_with_hotwords_and_a_call_out_wakes_only_that_agent(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """``/meeting`` joins as "switchboard (transcribing)" with the room's spoken names.
    A line that names claude-1 wakes claude-1, with to_you, and a line that names
    nobody wakes nobody. Fails if the header omits the hotwords, the line is stored
    as a person, or talk that names nobody starts a turn."""
    async with FakeAgent(broker.home, "k1") as claude, FakeAgent(broker.home, "k2") as codex:
        assert (await claude.join("#build", "claude-1"))["ok"]
        assert (await codex.join("#build", "codex-1"))["ok"]
        out = command(broker, f"/meeting {ZOOM}")
        assert "joining" in out["text"].lower() and SECRET not in out["text"]
        await wait_until(lambda: len(joinly.joins) == 1)
        join = joinly.joins[0]
        assert join["meeting_url"] == ZOOM
        assert join["participant_name"] == "switchboard (transcribing)"
        assert join["passcode"] is None
        settings = [json.loads(s) for s in joinly.settings]
        assert any(s.get("name") == "switchboard (transcribing)" for s in settings)
        words = [w.lower() for s in settings for w in s.get("stt_args", {}).get("hotwords", [])]
        assert "claude one" in words and "codex" in words
        waiting = codex.wait_task("#build", timeout_s=30)
        joinly.push(
            [
                seg("hello everyone", 0.0, 1.0),
            ]
        )
        await wait_until(lambda: len(meeting_lines(broker)) >= 1)
        assert not waiting.done()
        waiting.cancel()
        named = claude.wait_task("#build", timeout_s=30)
        joinly.push([seg("Claude one, tell me how much", 10.0, 12.0)])
        got = await named
        assert got.get("ok") and "to_you=yes" in got["text"] and "kind=meeting" in got["text"]
        assert "from=call" in got["text"]


async def test_a_line_that_is_only_the_hint_list_is_not_posted_and_wakes_nobody(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """#616, from a real call: the transcript had "Claude switchboard" (the transcriber writing back an
    agent's name and the bot's own) and then the real request. The echo was posted as a call line and
    woke claude-1 for nothing, which spent a wake. It is dropped now: the room has only the request, and
    the wake claude-1 gets is the request's, with no echo line in its batch. Fails if the echo is posted,
    or wakes the agent."""
    async with FakeAgent(broker.home, "k1") as claude:
        assert (await claude.join("#build", "claude-1"))["ok"]
        command(broker, f"/meeting {ZOOM}")
        await wait_until(lambda: len(joinly.joins) == 1)
        named = claude.wait_task("#build", timeout_s=30)
        joinly.push([seg("Claude switchboard", 0.0, 1.0)])
        joinly.push([seg("Claude one, can you find the upload size limit?", 10.0, 12.0)])
        got = await named
        assert got.get("ok") and "to_you=yes" in got["text"]
        assert "upload size limit" in got["text"] and "Claude switchboard" not in got["text"]
        assert [m["text"] for m in meeting_lines(broker)] == [
            "Claude one, can you find the upload size limit?"
        ]


async def test_an_echoed_lead_in_wakes_its_agent_and_only_the_real_words_are_posted(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """#602: "Okay, Claude one, can you hear me?" came out as "Claude-1 Claude switchboard Claude one
    can you hear me?". The agent is woken by the name before the words (the wake test is in
    test_meeting_names too), and the room reads "Claude one can you hear me?" without the echoed names.
    A line of names that no real words follow, in the same call, is not posted at all. Fails if the
    echoed words are in the posted line."""
    async with FakeAgent(broker.home, "k1") as claude:
        assert (await claude.join("#build", "claude-1"))["ok"]
        command(broker, f"/meeting {ZOOM}")
        await wait_until(lambda: len(joinly.joins) == 1)
        named = claude.wait_task("#build", timeout_s=30)
        joinly.push([seg("Claude Claude one switchboard", 0.0, 1.0)])
        joinly.push([seg("Claude-1 Claude switchboard Claude one can you hear me?", 10.0, 12.0)])
        got = await named
        assert got.get("ok") and "to_you=yes" in got["text"]
        assert 'text="Claude one can you hear me?"' in got["text"]
        assert [m["text"] for m in meeting_lines(broker)] == ["Claude one can you hear me?"]


async def test_an_ambiguous_name_asks_which_agent(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """A bare "Claude" with two Claudes posts the line and a notice, and wakes neither.
    Fails if one of them is guessed, or the notice names the wrong pair."""
    async with FakeAgent(broker.home, "k1") as a, FakeAgent(broker.home, "k2") as b:
        await a.join("#build", "claude-1")
        await b.join("#build", "claude-2")
        command(broker, "/meeting https://meet.google.com/abc-defg-hij")
        await wait_until(lambda: len(joinly.joins) == 1)
        wa, wb = a.wait_task("#build", timeout_s=30), b.wait_task("#build", timeout_s=30)
        joinly.push([seg("Claude, say the number", 0.0, 1.5)])
        await wait_until(lambda: "Say the number." in notices(broker))
        assert (
            'Someone on the call said "Claude", and this room has claude-1 and claude-2. Say the number.'
            in notices(broker)
        )
        assert not wa.done() and not wb.done()
        wa.cancel()
        wb.cancel()


async def test_leave_close_and_shutdown_each_leave_the_call(
    broker: InProcBroker, joinly: FakeJoinly, tmp_home: Path
) -> None:
    """Each way the call ends tells joinly to leave, so the bot does not stay as a ghost.
    Fails if one of the three paths only drops the client."""
    command(broker, "/meeting https://teams.microsoft.com/l/meetup-join/abc")
    await wait_until(lambda: len(joinly.joins) == 1)
    command(broker, "/meeting leave")
    await wait_until(lambda: joinly.leaves >= 1 and open_calls(broker) == 0)
    assert "stopped transcribing" in notices(broker)

    command(broker, "/meeting https://zoom.us/j/555")
    await wait_until(lambda: len(joinly.joins) == 2)
    command(broker, "/close")
    await wait_until(lambda: joinly.leaves >= 2 and open_calls(broker) == 0)

    web = broker.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=broker.write_headers()).status_code == 200
    broker.web = web  # type: ignore[attr-defined]
    command(broker, "/meeting https://teams.live.com/meet/abc")
    await wait_until(lambda: len(joinly.joins) == 3)
    broker.stop()
    await wait_until(lambda: joinly.leaves >= 3)
    # start again so the fixture's stop is a second, harmless one
    restarted = InProcBroker(tmp_home, broker.cfg).start()
    broker.server = restarted.server
    broker.thread = restarted.thread
    broker.app = restarted.app
    broker.tcp = restarted.tcp


async def test_a_link_off_the_allowlist_is_refused_and_the_passcode_is_nowhere(
    broker: InProcBroker, joinly: FakeJoinly, caplog: pytest.LogCaptureFixture
) -> None:
    """joinly's browser would open any URL, so anything but Zoom, Meet or Teams https
    is refused. The passcode is in the join and nowhere in the database, the notices
    or the log. Fails if an internal address is opened, or ``pwd=`` is stored or logged."""
    caplog.set_level(logging.DEBUG)
    for link in (
        "https://example.com/j/1",
        "http://zoom.us/j/1",
        "https://zoom.us.evil.example/j/1",
        "https://10.0.0.1/j/1",
    ):
        r = broker.web.post(  # type: ignore[attr-defined]
            "/api/rooms/build/command", json={"text": f"/meeting {link}"}, headers=broker.write_headers()
        )
        assert r.status_code == 400, r.text
        assert link not in r.text
    assert joinly.joins == []
    command(broker, f"/meeting {ZOOM}")
    await wait_until(lambda: len(joinly.joins) == 1)
    await wait_until(
        lambda: any(m.get("sender_kind") == "meeting" or "joining" in m["text"] for m in history(broker))
    )
    blob = Path(broker.paths.db).read_bytes()
    wal = Path(str(broker.paths.db) + "-wal")
    if wal.exists():
        blob += wal.read_bytes()
    assert SECRET.encode() not in blob
    assert SECRET not in notices(broker)
    assert SECRET not in caplog.text
    again = broker.web.post(  # type: ignore[attr-defined]
        "/api/rooms/build/command",
        json={"text": "/meeting https://meet.google.com/xyz-abcd-efg"},
        headers=broker.write_headers(),
    )
    assert again.status_code == 409
    assert "already" in again.text.lower()


async def test_an_agents_meeting_command_is_literal_text(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """An agent's ``say("/meeting ...")`` is chat, like every other command. Fails if
    an agent can start a call."""
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        said = await agent.say("#build", "/meeting https://zoom.us/j/999")
        assert said.get("ok")
    await wait_until(lambda: any("/meeting https://zoom.us/j/999" in m["text"] for m in history(broker)))
    assert joinly.joins == []
    assert all(m["sender_kind"] != "meeting" for m in history(broker))


def test_without_a_meeting_server_the_command_names_the_setup(tmp_home: Path) -> None:
    """No ``[meeting] joinly_url``: a short hint that names DEPLOY.md and both ways to set it (the
    config key, and ``SWITCHBOARD_JOINLY_URL`` for a manifest, #591), and no join.
    Fails if the command tries a default URL, or stays silent about how to set one."""
    b = InProcBroker(tmp_home, Config(human_name="alice")).start()
    web = b.web_client()
    try:
        assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
        r = web.post(
            "/api/rooms/build/command",
            json={"text": "/meeting https://zoom.us/j/1"},
            headers=b.write_headers(),
        )
        assert r.status_code == 200, r.text
        text = r.json()["text"]
        assert "DEPLOY.md" in text and "joinly_url" in text and "SWITCHBOARD_JOINLY_URL" in text
        con = sqlite3.connect(b.paths.db)
        assert con.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 0
        con.close()
    finally:
        web.close()
        b.stop()


async def test_a_join_failure_ends_the_call_and_still_leaves(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """joinly refusing the join marks the call ended, tells the room, and still calls
    leave_meeting. Fails if the room stays on 'joining' or the session is abandoned."""
    joinly.fail_join = True
    command(broker, "/meeting https://zoom.us/j/42")
    await wait_until(
        lambda: "couldn't join" in notices(broker).lower() or "could not" in notices(broker).lower()
    )
    await wait_until(lambda: joinly.leaves >= 1)
    con = sqlite3.connect(broker.paths.db)
    row = con.execute("SELECT state, error FROM meetings").fetchone()
    con.close()
    assert row[0] == "ended" and row[1]


LAYOFFS = "https://us02web.zoom.us/j/246813579?pwd=HOLDER77"
NEXT = "https://zoom.us/j/777000111?pwd=SECOND88"
REFUSAL = (
    "This broker follows at most 1 call at once, and that many are open."
    " Leave one first (/meeting leave in its room)."
)


async def test_past_the_limit_a_call_is_refused_and_nothing_reaches_joinly(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """With ``max_calls = 1`` (the default), a second room's ``/meeting`` is refused (409) with the
    limit's message, before a row is written or the link goes to joinly: the bot's memory holds
    one call (#607, DESIGN.md §52). The message is only the number: the room that holds the call
    and both links stay out of it, because the person asking may not see that room. The first
    call keeps going. Fails if the second call starts, a row is written for it, the refusal names
    a room or a link, or the limit check runs before the room's own ("already transcribing") one."""
    holder = make_room(broker, "layoffs-plan")
    command(broker, f"/meeting {LAYOFFS}", room=holder)
    await wait_until(lambda: len(joinly.joins) == 1 and call_states(broker) == ["live"])  # live counts

    refused = try_meeting(broker, NEXT, "build")
    assert refused.status_code == 409, refused.text
    assert refused.json()["message"] == REFUSAL  # the whole answer: the limit, no room, no link
    for private in ("layoffs", holder, "246813579", "HOLDER77", "777000111", "SECOND88", "zoom"):
        assert private not in refused.text, private
    assert meeting_rows(broker) == 1 and call_states(broker) == ["live"]  # no row for the refused one
    assert "joining" not in notices(broker).lower()  # and nothing was announced in #build

    # the room that holds the call is told "already", not the limit: its own check comes first
    again = try_meeting(broker, NEXT, holder)
    assert again.status_code == 409 and "already transcribing" in again.text and "at most" not in again.text

    # nothing was sent to joinly for the refused links: the only join is the first call's
    assert joinly.join_attempts == 1
    assert [j["meeting_url"] for j in joinly.joins] == [LAYOFFS]


async def test_leaving_the_call_frees_the_slot_for_another_room(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """``/meeting leave`` in the first room ends its call, and then the second room can start its own
    (#607): the limit counts calls that are joining or live, not calls ever started. Fails if the count
    never goes down (a leave leaves the slot taken) or the refused room stays refused."""
    holder = make_room(broker, "layoffs-plan")
    command(broker, f"/meeting {LAYOFFS}", room=holder)
    await wait_until(lambda: len(joinly.joins) == 1)
    assert try_meeting(broker, NEXT, "build").status_code == 409  # the slot is taken, so this test can fail

    command(broker, "/meeting leave", room=holder)
    await wait_until(lambda: joinly.leaves >= 1 and open_calls(broker) == 0)
    out = try_meeting(broker, NEXT, "build")
    assert out.status_code == 200, out.text
    await wait_until(lambda: len(joinly.joins) == 2)
    assert [j["meeting_url"] for j in joinly.joins] == [LAYOFFS, NEXT]  # the refused try never joined
    assert open_calls(broker) == 1


async def test_a_higher_limit_lets_that_many_rooms_start_and_refuses_the_next(
    broker_two_calls: InProcBroker, joinly: FakeJoinly
) -> None:
    """``max_calls = 2``: two rooms both start and a third is refused, in words that say 2 (#607).
    Fails if the limit is fixed at one, or if it is not enforced past the second."""
    b = broker_two_calls
    plan, ops = make_room(b, "plan"), make_room(b, "ops")
    assert try_meeting(b, LAYOFFS, "build").status_code == 200
    assert try_meeting(b, NEXT, plan).status_code == 200
    await wait_until(lambda: len(joinly.joins) == 2)
    third = try_meeting(b, "https://meet.google.com/abc-defg-hij", ops)
    assert third.status_code == 409, third.text
    assert "at most 2 calls at once, and that many are open" in third.text
    assert meeting_rows(b) == 2 and joinly.join_attempts == 2


async def test_a_call_that_is_joining_counts_and_one_that_failed_to_join_stops_counting(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """A call counts from the moment it is joining, before joinly has answered (the bot's browser is
    already starting), so a second room is refused while the first is held at the door; and when joinly
    then refuses the join, the call is over and the slot is free again (#607). Fails if only live calls
    count (the second room gets through while the first is joining), or if a failed join keeps its slot."""
    other = make_room(broker, "plan")
    joinly.hold_join = joinly.fail_join = True
    command(broker, f"/meeting {LAYOFFS}", room="build")
    await wait_until(lambda: joinly.join_attempts == 1 and call_states(broker) == ["joining"])
    assert try_meeting(broker, NEXT, other).status_code == 409  # a joining call holds the slot

    joinly.release_join()
    await wait_until(lambda: open_calls(broker) == 0)  # joinly said no: the call is over
    assert "couldn't join" in notices(broker).lower() or "could not" in notices(broker).lower()
    joinly.fail_join = False
    out = try_meeting(broker, NEXT, other)
    assert out.status_code == 200, out.text
    await wait_until(lambda: [j["meeting_url"] for j in joinly.joins] == [NEXT])
    assert call_states(broker) and open_calls(broker) == 1

"""A room's coordinator over the real broker (#976, DESIGN.md §50.2): a person names an agent from the web
session, and the agent runs the room actions they ticked with its one ``command`` tool, over HTTPS. The agents
are scripted MCP clients. Each test names the way it would fail."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly
from test_meetings import SECRET, ZOOM, notices, wait_until
from test_name_resume import (
    RESTARTED,
    THREAD_A,
    codex_session,
    join_code,
    killed_session_in_build,
    q,
    wait_for,
)

from switchboard.config import Config, MeetingCfg

PR = "https://example.com/shop/pull/7"
FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0, rate_limit_s=0.0)


def start(tmp_home: Path, cfg: Config) -> Iterator[InProcBroker]:
    b = InProcBroker(tmp_home, cfg).start()
    web = b.web_client()
    for room in ("#build", "#docs"):
        assert web.post("/api/rooms", json={"name": room}, headers=b.write_headers()).status_code == 200
    b.web = web  # type: ignore[attr-defined]
    yield b
    web.close()
    b.stop()


@pytest.fixture
def broker(tmp_home: Path) -> Iterator[InProcBroker]:
    yield from start(tmp_home, FAST)


def person(b: InProcBroker, text: str, room: str = "build", status: int = 200) -> dict:
    """A command typed in alice's signed-in browser."""
    r = b.web.post(  # type: ignore[attr-defined]
        f"/api/rooms/{room}/command", json={"text": text}, headers=b.write_headers()
    )
    assert r.status_code == status, r.text
    return r.json()


def room_quiet(b: InProcBroker, room: str = "#build") -> bool:
    r = b.on_loop(b.state.store.get_room, room)
    assert r is not None
    return r.quiet


async def run(agent: FakeAgent, text: str, room: str = "#build") -> dict:
    return await agent.call("command", room=room, text=text)


async def test_a_coordinator_with_quiet_ticked_turns_the_room_quiet_and_the_notice_names_both(
    broker: InProcBroker,
) -> None:
    """The issue's done-when: a person names an agent with ``quiet`` ticked, it runs
    ``command("/room quiet on")``, the room goes quiet and the notice reads "lead-1 for alice ...". The person
    undoes it as before. Fails if the tool is missing, the setting does not change, or a notice names one."""
    async with FakeAgent(broker.home, "k1") as lead:
        assert "command" in {t.name for t in await lead.list_tools()}
        joined = await lead.join("#build", "lead-1")
        assert "coordinator" not in joined["text"]
        person(broker, "/coordinator lead-1 quiet")
        got = await run(lead, "/room quiet on")
        assert got["ok"] and "quiet room is on" in got["text"], got
        assert room_quiet(broker)
        assert "lead-1 for alice turned on a quiet room" in notices(broker)
        person(broker, "/room quiet off")  # the person undoes it, as before
        assert not room_quiet(broker)
        assert (await run(lead, "/quiet lead-1"))["ok"]
        assert (await run(lead, "/unquiet lead-1"))["ok"]


async def test_what_is_not_ticked_and_everyone_else_is_refused_and_nothing_changes(
    broker: InProcBroker,
) -> None:
    """With ``pins`` ticked, ``/room quiet on`` is refused and says what is allowed; another agent in the room
    is refused; the coordinator of #docs is refused in #build; a coordinator's ``/coordinator`` is refused (it
    cannot name one or change its own actions). Fails if any of them changes a setting or the coordinator."""
    async with (
        FakeAgent(broker.home, "k1") as lead,
        FakeAgent(broker.home, "k2") as bob,
        FakeAgent(broker.home, "k3") as docs_lead,
    ):
        await lead.join("#build", "lead-1")
        await bob.join("#build", "bob-1")
        await docs_lead.join("#docs", "docs-1")
        await docs_lead.join("#build", "docs-1b")
        person(broker, "/coordinator lead-1 pins")
        person(broker, "/coordinator docs-1 quiet,pins,boards,calls,budget", room="docs")
        refused = await run(lead, "/room quiet on")
        assert (
            not refused["ok"] and refused["code"] == "forbidden" and "quiet is not ticked" in refused["error"]
        )
        assert "/pin <id>" in refused["error"]
        for who, text, room in (
            (bob, "/room quiet on", "#build"),
            (docs_lead, "/room quiet on", "#build"),  # the coordinator of another room, here
            (lead, "/coordinator lead-1 quiet", "#build"),
            (lead, "/coordinator bob-1 quiet,pins", "#build"),
            (lead, "/coordinator off", "#build"),
        ):
            out = await run(who, text, room)
            assert not out["ok"] and out["code"] == "forbidden", (text, out)
        assert not room_quiet(broker) and not room_quiet(broker, "#docs")
        co = broker.on_loop(
            broker.state.store.coordinator, broker.on_loop(broker.state.store.get_room, "#build").id
        )
        assert co is not None and co.name == "lead-1" and co.actions == ("pins",)
        assert person(broker, "/coordinator")["text"].count("lead-1") == 1


async def test_an_agents_say_of_coordinator_is_literal_text(broker: InProcBroker) -> None:
    """An agent's ``say("/coordinator ...")`` is stored as text like every command (§10). Fails if a room
    message can name a coordinator."""
    async with FakeAgent(broker.home, "k1") as bob:
        await bob.join("#build", "bob-1")
        assert (await bob.say("#build", "/coordinator bob-1 quiet,pins,boards,calls,budget"))["ok"]
    co = broker.on_loop(
        broker.state.store.coordinator, broker.on_loop(broker.state.store.get_room, "#build").id
    )
    assert co is None


async def test_with_boards_ticked_it_closes_a_board_it_did_not_open_and_without_it_it_asks(
    broker: InProcBroker,
) -> None:
    """Without ``boards``, a close of another agent's board only asks a person (as today); with it, the board
    closes and the line says "closed by lead-1 for alice". Fails if either half is the other's behavior."""
    async with FakeAgent(broker.home, "k1") as lead, FakeAgent(broker.home, "k2") as author:
        await lead.join("#build", "lead-1")
        await author.join("#build", "claude-1")
        await author.call("review", room="#build", action="open", url=PR, head="abc1234")
        person(broker, "/coordinator lead-1 quiet")
        no = await lead.call("review", room="#build", action="close", url=PR, reason="shipped")
        assert not no["ok"] and "only claude-1 or a person can close it" in no["error"]
        person(broker, "/coordinator lead-1 boards")
        yes = await lead.call("review", room="#build", action="close", url=PR, reason="merged and shipped")
        assert yes["ok"] and yes["closed"], yes
        assert f"Review board for {PR} closed by lead-1 for alice: merged and shipped" in notices(broker)


async def test_a_board_close_needs_a_reason_and_is_the_coordinators_alone(broker: InProcBroker) -> None:
    """A third agent is not helped by someone else being the coordinator, and the coordinator's close of a
    board it does not hold needs the reason a person's gives. Fails if either is waived."""
    async with (
        FakeAgent(broker.home, "k1") as lead,
        FakeAgent(broker.home, "k2") as author,
        FakeAgent(broker.home, "k3") as third,
    ):
        await lead.join("#build", "lead-1")
        await author.join("#build", "claude-1")
        await third.join("#build", "codex-1")
        await author.call("review", room="#build", action="open", url=PR, head="abc1234")
        person(broker, "/coordinator lead-1 boards")
        bare = await lead.call("review", room="#build", action="close", url=PR)
        assert not bare["ok"] and "reason" in bare["error"]
        other = await third.call("review", room="#build", action="close", url=PR, reason="mine now")
        assert not other["ok"] and "only claude-1 or a person can close it" in other["error"]


async def test_the_join_text_and_who_say_who_the_coordinator_is_and_what_it_may_run(
    broker: InProcBroker,
) -> None:
    """The coordinator's own join text names its tool and what it may run; another agent's names the
    coordinator, and ``who`` shows it. Fails if either is silent, or other agents are told to obey it."""
    async with FakeAgent(broker.home, "k1") as lead, FakeAgent(broker.home, "k2") as bob:
        await lead.join("#build", "lead-1")
        person(broker, "/coordinator lead-1 quiet,pins")
        again = await lead.call("join", room="#build", screen_name="lead-1")
        assert "You are this room's coordinator, named by alice" in again["text"]
        assert "/room quiet on|off" in again["text"] and "/pin <id>" in again["text"]
        assert "/meeting" not in again["text"].split("coordinator")[1].split("Pass")[0]
        joined = await bob.join("#build", "bob-1")
        assert (
            "lead-1 is this room's coordinator: alice let it run quiet, pins with command()" in joined["text"]
        )
        assert "peer" in joined["text"]
        assert "coordinator=quiet,pins" in (await bob.who("#build"))["text"]


async def test_a_restart_that_resumes_keeps_the_role_and_a_leave_does_not(broker: InProcBroker) -> None:
    """A session killed without its hooks and resumed with its code keeps the role on its new membership
    and can run its command; an agent that leaves and resumes does not get it back; neither does a person's
    removal. Fails if a resume drops the role (as #974's quiet once was), or a leave leaves it for the
    code."""
    code = killed_session_in_build(broker)
    person(broker, "/coordinator planner quiet")
    new = codex_session(broker)
    assert new.call("join", RESTARTED, room="#build", screen_name="planner", resume=code)["ok"]
    co = broker.on_loop(
        broker.state.store.coordinator, broker.on_loop(broker.state.store.get_room, "#build").id
    )
    assert co is not None and co.name == "planner" and co.actions == ("quiet",)
    assert new.call("command", RESTARTED, room="#build", text="/room quiet on")["ok"]
    assert room_quiet(broker)
    again = new.call("join", RESTARTED, room="#build", screen_name="planner")
    assert "You are this room's coordinator" in again["text"]
    assert new.call("leave", RESTARTED, room="#build")["ok"]
    assert wait_for(lambda: q(broker, "SELECT 1 FROM memberships WHERE left_reason='leave'"))
    assert q(broker, "SELECT COUNT(*) FROM room_coordinators")[0][0] == 0
    last = codex_session(broker)
    got = last.call("join", THREAD_A, room="#build", screen_name="planner", resume=join_code(again))
    assert got["ok"] and "coordinator" not in got["text"]
    out = last.call("command", THREAD_A, room="#build", text="/room quiet off")
    assert not out["ok"] and out["code"] == "forbidden"
    assert room_quiet(broker)


@pytest.fixture
def joinly() -> Iterator[FakeJoinly]:
    server = FakeJoinly().start()
    yield server
    server.stop()


@pytest.fixture
def calls_broker(tmp_home: Path, joinly: FakeJoinly) -> Iterator[InProcBroker]:
    yield from start(tmp_home, FAST.replace(meeting=MeetingCfg(joinly_url=joinly.url, max_calls=1)))


async def test_a_coordinators_call_link_is_in_no_reply_notice_event_log_or_database(
    calls_broker: InProcBroker, joinly: FakeJoinly, caplog: pytest.LogCaptureFixture
) -> None:
    """``/meeting <link>`` from the coordinator joins the call, and the link and its passcode are in no
    reply, refusal, notice, event, log line or database byte, as a person's own (§52, §54). ``/meeting
    <link> mine`` stays a person's. Fails if any of them carries the passcode or the address."""
    caplog.set_level(logging.DEBUG)
    b = calls_broker
    async with FakeAgent(b.home, "k1") as lead:
        await lead.join("#build", "lead-1")
        person(b, "/coordinator lead-1 calls")
        replies = [
            await run(lead, "/meeting https://example.com/j/1?pwd=" + SECRET),  # off the allowlist
            await run(lead, f"/meeting {ZOOM} mine"),  # a person's own agent hosts: not the coordinator's
            await run(lead, f"/meeting {ZOOM}"),
            await run(lead, "/meeting https://meet.google.com/xyz-abcd-efg"),  # a second call: refused
            await run(lead, "/meeting voice on"),
            await run(lead, "/meeting leave"),
        ]
        assert replies[0]["code"] == "bad_request" and replies[1]["code"] == "forbidden"
        assert replies[2]["ok"] and "joining" in replies[2]["text"]
        await wait_until(lambda: len(joinly.joins) == 1)
        assert joinly.joins[0]["meeting_url"] == ZOOM
        await wait_until(
            lambda: not b.on_loop(b.state.store.open_meeting, b.on_loop(b.state.store.get_room, "#build").id)
        )
    blob = json.dumps(replies) + notices(b)
    blob += json.dumps([e.data for e in b.on_loop(lambda: b.state.store.recent_events(limit=500))])
    assert "lead-1 for alice is joining a" in notices(b)
    db_bytes = Path(b.paths.db).read_bytes()
    wal = Path(str(b.paths.db) + "-wal")
    if wal.exists():
        db_bytes += wal.read_bytes()
    for needle in (SECRET, "123456789", "zoom.us", "example.com/j/1"):
        assert needle not in blob, needle
        assert needle.encode() not in db_bytes, needle
        assert needle not in caplog.text, needle


def rows(b: InProcBroker, sql: str) -> list[sqlite3.Row]:
    return q(b, sql)


async def test_the_hourly_limit_is_per_coordinator_over_the_tool(broker: InProcBroker) -> None:
    """The 31st command in the hour is refused over the tool, with when to try again, and the room's setting
    stays as the 30th left it. (The window itself is advanced with a ``FakeClock`` in the unit tests.) Fails
    if the limit is not enforced on the tool path."""
    async with FakeAgent(broker.home, "k1") as lead:
        await lead.join("#build", "lead-1")
        person(broker, "/coordinator lead-1 quiet")
        for _ in range(30):
            assert (await run(lead, "/unquiet lead-1"))["ok"]
        out = await run(lead, "/room quiet on")
        assert not out["ok"] and out["code"] == "rate_limited" and "30 commands an hour" in out["error"]
        assert not room_quiet(broker)

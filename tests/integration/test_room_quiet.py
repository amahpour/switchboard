"""A quiet room: agents that join hear only what is addressed to them (#975, DESIGN.md §50.1).

The broker is the real one and the agents are scripted MCP clients. What each test pins is the thing a
person turns this on for: an unaddressed message wakes nobody, and a mention or a reply still wakes its
addressee. Every test names the way it would fail.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from test_meetings import command, notices

from switchboard.config import Config
from switchboard.envelope import QUIET_JOIN_NOTE

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0, rate_limit_s=0.0)


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


def quiet_of(b: InProcBroker, name: str) -> bool:
    room = b.state.store.get_room("#build")
    assert room is not None
    member = b.state.store.find_member(room.id, name)
    assert member is not None
    return member.quiet


def room_quiet(b: InProcBroker) -> bool:
    r = b.web.get("/api/rooms/build/members", headers=b.write_headers())
    assert r.status_code == 200, r.text
    return r.json()["settings"]["quiet"]


async def test_an_agent_that_joins_a_quiet_room_is_quiet_and_is_told(broker: InProcBroker) -> None:
    """With the room's setting on, a joining agent is quiet and its join text says so; in a room without
    it, it is not and the text is silent. Fails if the join ignores the setting or the text omits the note."""
    command(broker, "/room quiet on")
    async with FakeAgent(broker.home, "k1") as a:
        joined = await a.join("#build", "alpha")
        assert QUIET_JOIN_NOTE in joined["text"]
        assert quiet_of(broker, "alpha")
    command(broker, "/room quiet off")
    async with FakeAgent(broker.home, "k2") as b:
        joined = await b.join("#build", "beta")
        assert QUIET_JOIN_NOTE not in joined["text"]
        assert not quiet_of(broker, "beta")


async def test_in_a_quiet_room_unaddressed_chatter_wakes_nobody_and_a_mention_still_does(
    broker: InProcBroker,
) -> None:
    """An agent waiting in a quiet room is not woken by another agent's unaddressed message, and is by an
    @mention. Fails if the chatter ends the wait, or the mention is held back."""
    command(broker, "/room quiet on")
    async with FakeAgent(broker.home, "k1") as a, FakeAgent(broker.home, "k2") as b:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        waiting = a.wait_task("#build", 20)
        await asyncio.sleep(0.4)
        await b.say("#build", "just thinking aloud")
        done, _ = await asyncio.wait([waiting], timeout=1.5)
        assert not done, "unaddressed chatter woke a quiet agent"
        assert (await b.say("#build", "@alpha a question for you"))["ok"]
        woke = await asyncio.wait_for(waiting, 5)
        assert woke["status"] == "messages" and "a question for you" in woke["text"]
        # the chatter it was not woken for rides along, so nothing is lost
        assert "thinking aloud" in woke["text"]


async def test_in_a_quiet_room_a_reply_to_an_agents_own_message_still_wakes_it(broker: InProcBroker) -> None:
    """The other half of the addressed rule: a reply to the quiet agent's message wakes it, an unaddressed
    line in between does not. Fails if the reply is held as chatter."""
    command(broker, "/room quiet on")
    async with FakeAgent(broker.home, "k1") as a, FakeAgent(broker.home, "k2") as b:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        mine = (await a.say("#build", "alpha here, any news?"))["posted_id"]
        waiting = a.wait_task("#build", 20)
        await asyncio.sleep(0.4)
        await b.say("#build", "just thinking aloud")
        done, _ = await asyncio.wait([waiting], timeout=1.5)
        assert not done, "unaddressed chatter woke a quiet agent"
        await b.say("#build", "answering you", reply_to=mine)
        woke = await asyncio.wait_for(waiting, 5)
        assert woke["status"] == "messages" and "answering you" in woke["text"]


async def test_the_same_chatter_wakes_an_agent_in_a_room_without_the_setting(broker: InProcBroker) -> None:
    """The control for the test above: with the setting off, the unaddressed message ends the wait. Fails
    if the quiet room's silence is not the setting's doing."""
    async with FakeAgent(broker.home, "k1") as a, FakeAgent(broker.home, "k2") as b:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        waiting = a.wait_task("#build", 20)
        await asyncio.sleep(0.4)
        await b.say("#build", "just thinking aloud")
        woke = await asyncio.wait_for(waiting, 5)
        assert woke["status"] == "messages" and "thinking aloud" in woke["text"]


async def test_turning_it_on_quiets_the_agents_here_and_turning_it_off_leaves_them(
    broker: InProcBroker,
) -> None:
    """On quiets every agent already in the room, a person's ``/unquiet`` still lifts one agent, and off
    leaves each agent as it is, a new one then joining not quiet. Asking again changes nothing and says so.
    Fails if on skips an agent already here, off un-quiets one, ``/unquiet`` is refused, or a notice is
    missing."""
    async with FakeAgent(broker.home, "k1") as a, FakeAgent(broker.home, "k2") as b:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        assert not quiet_of(broker, "alpha") and not room_quiet(broker)
        assert "already off" in command(broker, "/room quiet off")["text"]
        command(broker, "/room quiet on")
        assert room_quiet(broker) and quiet_of(broker, "alpha") and quiet_of(broker, "beta")
        assert "alice turned on a quiet room" in notices(broker)
        assert "already on" in command(broker, "/room quiet on")["text"]
        command(broker, "/unquiet beta")
        assert quiet_of(broker, "alpha") and not quiet_of(broker, "beta")
        command(broker, "/room quiet off")
        assert not room_quiet(broker) and quiet_of(broker, "alpha") and not quiet_of(broker, "beta")
        assert "agents here join quiet: off" in command(broker, "/room")["text"]
        async with FakeAgent(broker.home, "k3") as c:
            await c.join("#build", "gamma")
            assert not quiet_of(broker, "gamma")


async def test_an_agents_say_of_the_command_changes_nothing(broker: InProcBroker) -> None:
    """The command is a person's: an agent's ``say("/room quiet on")`` is stored as text. Fails if the room
    gains the setting or a notice."""
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "alpha")
        assert (await a.say("#build", "/room quiet on"))["ok"]
        assert not room_quiet(broker) and not quiet_of(broker, "alpha")
        assert "quiet room" not in notices(broker)


def test_a_bad_quiet_command_is_refused(broker: InProcBroker) -> None:
    """``/room quiet`` needs on or off. Fails if it is guessed at."""
    for text in ("/room quiet", "/room quiet maybe", "/room quiet on now"):
        r = broker.web.post("/api/rooms/build/command", json={"text": text}, headers=broker.write_headers())
        assert r.status_code == 400 and "usage: /room" in r.text

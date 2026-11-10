"""Turn-taking through the real server bridge and hosted-call key routes (DESIGN.md §52.2)."""

from __future__ import annotations

import pytest
from conftest import FakeClock, InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly
from test_meeting_hosted import SPEECH, World, hosted_call
from test_meeting_hosted import world as world
from test_meeting_voice import joinly as joinly
from test_meeting_voice import start_call
from test_meeting_voice import timed as timed
from test_meetings import command, notices, seg, wait_until


def advance(b: InProcBroker, clock: FakeClock, seconds: float, *, heard: bool = False) -> None:
    """Advance policy time and notify the waiting worker, without sleeping to mean 'by now'."""

    def move():
        clock.advance(seconds)
        live = b.state.meetings._live[b.state.store.get_room("#build").id]
        if heard:
            live.turns.hear(clock.now())
        live.changed.set()

    b.on_loop(move)


async def test_bridge_holds_for_a_gap_drops_stale_and_refuses_a_second_line(
    timed: tuple[InProcBroker, FakeClock], joinly: FakeJoinly
) -> None:
    """The real say stays a room post; a refused speaker receives its fixed notice in a tool batch."""
    b, clock = timed
    async with FakeAgent(b.home, "k1") as a, FakeAgent(b.home, "k2") as other:
        await a.join("#build", "alpha")
        await other.join("#build", "beta")
        await start_call(b, joinly)
        command(b, "/meeting voice on")
        command(b, "/meeting listen alpha")
        joinly.push([seg("People are talking.", 0, 1)])
        await wait_until(lambda: b.on_loop(lambda: b.state.meetings._live[1].turns.last_heard == clock.now()))
        await a.say("#build", "@call First answer.")
        assert not joinly.spoken
        refused = await other.say("#build", "@call Second answer.")
        assert refused.get("posted_id")
        received = await other.read("#build")
        assert "waiting or being spoken" in received["text"]
        advance(b, clock, 1.5)
        await wait_until(lambda: joinly.spoken == ["First answer."])
        await wait_until(lambda: b.on_loop(lambda: b.state.meetings._live[1].turns.active is None))
        await a.say("#build", "@call Stale answer.")
        # Every new speech pushes the gap past the queued line's expiry.
        for _ in range(6):
            advance(b, clock, 10, heard=True)
        advance(b, clock, 0)
        await wait_until(lambda: "waited 60 seconds" in notices(b))
        assert joinly.spoken == ["First answer."]


async def test_hosted_turn_completion_and_thirty_second_interval(world: World) -> None:
    """A poll claims one line until this call's relay reports its matching completion id."""
    w = world
    a, other = await hosted_call(w)
    try:
        w.command("/meeting voice on")
        await a.say("#build", "@call First.")
        first = w.relay.speech().json()
        assert first["lines"] == ["First."] and type(first["speech_id"]) is int
        refused = await other.say("#build", "@call Refused while speaking.")
        assert refused.get("posted_id")
        received = await other.read("#build")
        assert "waiting or being spoken" in received["text"]
        w.clock.advance(31)
        w.relay.post(SPEECH, {"wait": 0, "done": first["speech_id"] + 1})
        await other.say("#build", "@call Still refused.")
        assert w.relay.speech().json()["lines"] == []
        done = w.relay.post(SPEECH, {"wait": 0, "done": first["speech_id"]})
        assert done.status_code == 200
        await a.say("#build", "@call Next.")
        second = w.relay.speech().json()
        assert second["lines"] == ["Next."]
        assert second["speech_id"] != first["speech_id"]
        w.relay.post(SPEECH, {"wait": 0, "done": second["speech_id"]})
        await a.say("#build", "@call Wait for the interval.")
        assert w.relay.speech().json()["lines"] == []
        w.clock.advance(30)
        assert w.relay.speech().json()["lines"] == ["Wait for the interval."]
    finally:
        await a.close()
        await other.close()


@pytest.mark.parametrize("done", [True, "1", 0, -1, 2**63, {}, []])
async def test_hosted_refuses_malformed_completion_ids(world: World, done: object) -> None:
    a, other = await hosted_call(world)
    try:
        assert world.relay.post(SPEECH, {"wait": 0, "done": done}).status_code == 400
    finally:
        await a.close()
        await other.close()


async def test_hosted_speech_waits_for_a_gap_and_expires_while_people_keep_talking(world: World) -> None:
    """Raw participant segments, not room notices, control both sources' speech gap."""
    from test_meeting_hosted import seg as hosted_segment

    w = world
    a, other = await hosted_call(w)
    try:
        w.command("/meeting voice on")
        w.relay.push([hosted_segment("Still talking.", 0, 1)])
        await a.say("#build", "@call Answer.")
        assert w.relay.speech().json()["lines"] == []
        w.tick(1.5)
        first = w.relay.speech().json()
        assert first["lines"] == ["Answer."]
        w.relay.post(SPEECH, {"wait": 0, "done": first["speech_id"]})
        await a.say("#build", "@call Stale.")
        for i in range(6):
            w.tick(10)
            w.relay.push([hosted_segment("More talk.", 10 + i * 2, 11 + i * 2)])
            assert w.relay.speech().json()["lines"] == []
        received = a.claude.call("read", room="#build")
        assert "waited 60 seconds" in received["text"]
    finally:
        await a.close()
        await other.close()

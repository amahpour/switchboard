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


def poll(w: World, **body: object) -> dict:
    """A speech poll from a relay that reports when each line ends, as every relay since #697 does."""
    return w.relay.post(SPEECH, {"wait": 0, "turns": True, **body}).json()


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
        first = poll(w)
        assert first["lines"] == ["First."] and type(first["speech_id"]) is int
        refused = await other.say("#build", "@call Refused while speaking.")
        assert refused.get("posted_id")
        received = await other.read("#build")
        assert "waiting or being spoken" in received["text"]
        w.clock.advance(31)
        poll(w, done=first["speech_id"] + 1)
        await other.say("#build", "@call Still refused.")
        assert poll(w)["lines"] == []
        assert w.relay.post(SPEECH, {"wait": 0, "turns": True, "done": first["speech_id"]}).status_code == 200
        await a.say("#build", "@call Next.")
        second = poll(w)
        assert second["lines"] == ["Next."]
        assert second["speech_id"] != first["speech_id"]
        poll(w, done=second["speech_id"])
        await a.say("#build", "@call Wait for the interval.")
        assert poll(w)["lines"] == []
        w.clock.advance(30)
        assert poll(w)["lines"] == ["Wait for the interval."]
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
        assert poll(w)["lines"] == []
        w.tick(1.5)
        first = poll(w)
        assert first["lines"] == ["Answer."]
        poll(w, done=first["speech_id"])
        await a.say("#build", "@call Stale.")
        for i in range(6):
            w.tick(10)
            w.relay.push([hosted_segment("More talk.", 10 + i * 2, 11 + i * 2)])
            assert poll(w)["lines"] == []
        received = a.claude.call("read", room="#build")
        assert "waited 60 seconds" in received["text"]
    finally:
        await a.close()
        await other.close()


async def test_an_older_relay_that_never_reports_an_end_is_paced_not_silenced(world: World) -> None:
    """A relay from before #697 polls without "turns" and never sends "done". Its slot is freed when it takes
    a line, so its call is paced by the 30 s interval, as a relay is paced on a broker that has no turns.
    Fails if such a call falls silent after its first line until someone restarts the relay."""
    w = world
    a, other = await hosted_call(w)
    try:
        w.command("/meeting voice on")
        await a.say("#build", "@call First.")
        assert w.relay.speech().json()["lines"] == ["First."]
        await a.say("#build", "@call Second.")
        assert w.relay.speech().json()["lines"] == []
        w.clock.advance(30)
        assert w.relay.speech().json()["lines"] == ["Second."]
    finally:
        await a.close()
        await other.close()


@pytest.mark.parametrize("turns", ["yes", 1, 0, None, {}, []])
async def test_hosted_refuses_a_turns_claim_that_is_not_a_boolean(world: World, turns: object) -> None:
    """Only true or false says what a relay reports; anything else is refused, never read as either."""
    a, other = await hosted_call(world)
    try:
        assert world.relay.post(SPEECH, {"wait": 0, "turns": turns}).status_code == 400
    finally:
        await a.close()
        await other.close()

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
        assert "already waiting" in received["text"]
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
        queued = await other.say("#build", "@call Second agent.")
        assert queued.get("posted_id")
        assert poll(w)["lines"] == []  # the first line is still being spoken: no overlap
        w.clock.advance(31)
        assert poll(w, done=first["speech_id"] + 1)["lines"] == []  # a wrong id frees nothing
        again = w.relay.post(SPEECH, {"wait": 0, "turns": True, "done": first["speech_id"]}).json()
        assert again["lines"] == ["Second agent."]  # another agent has no start of its own to wait out
        await a.say("#build", "@call Next.")
        assert poll(w)["lines"] == []  # waits behind the second agent's line
        nxt = poll(w, done=again["speech_id"])
        assert nxt["lines"] == ["Next."]  # a's own 30 s since its first start has passed
        await a.say("#build", "@call Wait for the interval.")
        assert poll(w, done=nxt["speech_id"])["lines"] == []
        w.clock.advance(29)
        assert poll(w)["lines"] == []
        w.clock.advance(1)
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


def alice_says(w: World, text: str) -> None:
    r = w.web.post("/api/rooms/build/say", json={"text": text}, headers=w.b.write_headers())
    assert r.status_code == 200, r.text


async def test_an_answer_to_a_question_put_to_its_speaker_starts_after_two_seconds_once_per_batch(
    world: World,
) -> None:
    """The broker, not the line's words, marks an answer: the agent's last batch held an @mention of it. That
    line starts 2 s after the last start; the next line from the same batch, or one from a batch with no
    question, waits its agent's own 30 s (#713). Fails if every line takes the 2 s door, if the door is
    opened by what the line says, or if one batch opens it twice."""
    w = world
    a, other = await hosted_call(w)
    try:
        w.command("/meeting voice on")
        await other.say("#build", "@call Hello.")
        hello = poll(w)
        assert hello["lines"] == ["Hello."]
        poll(w, done=hello["speech_id"])
        w.clock.advance(5)
        alice_says(w, "@codex-1 are you sure?")
        await other.read("#build")
        await other.say("#build", "@call Yes, I am sure. I am answering your question.")
        yes = poll(w)
        # only 5 s after codex's own first start
        assert yes["lines"] == ["Yes, I am sure. I am answering your question."]
        poll(w, done=yes["speech_id"])
        await other.say("#build", "@call And another thing, same batch.")
        w.clock.advance(29)
        assert poll(w)["lines"] == []  # the batch answered once; 29 s since this agent's start
        w.clock.advance(1)
        assert poll(w)["lines"] == ["And another thing, same batch."]
    finally:
        await a.close()
        await other.close()


async def test_an_answer_does_not_jump_the_two_second_wait_after_the_last_start(world: World) -> None:
    """Even a marked answer waits 2 s from the call's last start. Fails if the answer starts at once."""
    w = world
    a, other = await hosted_call(w)
    try:
        w.command("/meeting voice on")
        await a.say("#build", "@call First.")
        first = poll(w)
        poll(w, done=first["speech_id"])
        alice_says(w, "@codex-1 are you there?")
        await other.read("#build")
        w.clock.advance(0.5)
        await other.say("#build", "@call Here.")
        assert poll(w)["lines"] == []  # 0.5 s since the last start
        w.clock.advance(1.4)
        assert poll(w)["lines"] == []  # 1.9 s
        w.clock.advance(0.1)
        assert poll(w)["lines"] == ["Here."]
    finally:
        await a.close()
        await other.close()


async def test_four_agent_lines_in_a_row_then_a_person_writes_in_the_room(world: World) -> None:
    """The fifth line is refused with a fixed notice, and is spoken again once a person writes in the
    room. Fails if the run is unbounded or only call speech ends it."""
    w = world
    a, other = await hosted_call(w)
    try:
        w.command("/meeting voice on")
        for i, who in enumerate((a, other, a, other)):
            await who.say("#build", f"@call Line {i}.")
            line = poll(w)
            assert line["lines"] == [f"Line {i}."]
            poll(w, done=line["speech_id"])
            w.clock.advance(31)
        await a.say("#build", "@call Fifth.")
        assert poll(w)["lines"] == []
        assert any("4 lines in a row" in n for n in w.notices())
        alice_says(w, "Carry on.")
        await a.say("#build", "@call Sixth.")
        assert poll(w)["lines"] == ["Sixth."]
    finally:
        await a.close()
        await other.close()


# ------------------------------------------------------------------------------ a voice per line (#713)
async def test_a_hosted_line_carries_its_speakers_voice_and_a_missing_voice_is_told_once(
    world: World,
) -> None:
    """The speech poll's answer names the voice of the line (``line_voice``): the member's default, or the
    one a person pinned. A receipt that says Kokoro lacked it makes one room notice for that voice, naming the
    member,
    however many lines follow. Fails if the field is absent, ignores a pin, or the notice repeats."""
    from switchboard.meeting import voices

    w = world
    a, other = await hosted_call(w)
    try:
        w.command("/meeting voice on")
        await a.say("#build", "@call First.")
        first = poll(w)
        assert first["lines"] == ["First."] and first["line_voice"] == voices.default_for("claude-1")
        w.command("/meeting voice claude-1 zz_nobody")
        # a receipt that does not say it changes nothing
        poll(w, done=first["speech_id"])
        assert not any("was not found" in n for n in w.notices())
        w.clock.advance(31)
        await a.say("#build", "@call Second.")
        second = poll(w)
        assert second["lines"] == ["Second."] and second["line_voice"] == "zz_nobody"
        poll(w, done=second["speech_id"], voice_missing=True)
        notice = voices.MISSING_NOTICE.format(name="claude-1", voice="zz_nobody")
        assert w.notices().count(notice) == 1
        w.clock.advance(31)
        await a.say("#build", "@call Third.")
        third = poll(w)
        poll(w, done=third["speech_id"], voice_missing=True)
        assert w.notices().count(notice) == 1  # once a call for that voice
        # a receipt for another id, or with no line, names nothing
        poll(w, done=999, voice_missing=True)
        assert w.notices().count(notice) == 1
    finally:
        await a.close()
        await other.close()


@pytest.mark.parametrize("flag", ["yes", 1, 0, None, {}, []])
async def test_hosted_refuses_a_voice_missing_that_is_not_a_boolean(world: World, flag: object) -> None:
    """Only true or false says what a relay found; anything else is refused, never read as either."""
    a, other = await hosted_call(world)
    try:
        assert world.relay.post(SPEECH, {"wait": 0, "done": 1, "voice_missing": flag}).status_code == 400
    finally:
        await a.close()
        await other.close()

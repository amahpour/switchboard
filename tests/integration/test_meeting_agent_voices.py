"""A voice for each agent on a call (#713, DESIGN.md §52.6).

The real broker with ``FakeJoinly``, which records the ``voice`` each ``speak_text`` was asked for. Each agent
gets a default from its screen name; a person pins another with ``/meeting voice <name> <voice>``; an agent's
own words never set one. Time is the broker's ``FakeClock``; nothing sleeps.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FakeClock, InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly
from test_meeting_voice import advance_voice, make_broker, refused, speech_finished, start_call
from test_meetings import command, history, wait_until

from switchboard.meeting import voices


@pytest.fixture
def joinly() -> Iterator[FakeJoinly]:
    server = FakeJoinly().start()
    yield server
    server.stop()


@pytest.fixture
def timed(tmp_home: Path, joinly: FakeJoinly) -> Iterator[tuple[InProcBroker, FakeClock]]:
    clock = FakeClock()
    b = make_broker(tmp_home, joinly, clock=clock)
    yield b, clock
    b.web.close()  # type: ignore[attr-defined]
    b.stop()


def members(b: InProcBroker) -> dict[str, dict]:
    r = b.web.get("/api/rooms/build/members", headers=b.write_headers())  # type: ignore[attr-defined]
    return {m["name"]: m for m in r.json()["members"]}


async def say_and_wait(
    b: InProcBroker, clock: FakeClock, joinly: FakeJoinly, agent: FakeAgent, text: str
) -> None:
    before = len(joinly.spoken)
    advance_voice(b, clock, 31)  # past the speaker's own interval and the speech gap
    await agent.say("#build", f"@call {text}")
    await wait_until(lambda: len(joinly.spoken) == before + 1)
    await wait_until(lambda: speech_finished(b))


async def test_each_agent_speaks_in_the_voice_its_name_picks_and_a_pin_changes_it(
    timed: tuple[InProcBroker, FakeClock], joinly: FakeJoinly
) -> None:
    """Without any setting two agents are spoken in the defaults their names give (the same on every
    broker), Members shows them, a pin changes one and says so, and ``default`` puts the first back. Fails if
    the voice is not sent with the line, is the same for every agent, or a pin misses the next line."""
    b, clock = timed
    async with FakeAgent(b.home, "k1") as one, FakeAgent(b.home, "k2") as two:
        await one.join("#build", "claude-1")
        await two.join("#build", "codex-1")
        await start_call(b, joinly)
        command(b, "/meeting voice on")
        shown = members(b)
        assert shown["claude-1"]["voice"] == voices.default_for("claude-1")
        assert shown["codex-1"]["voice"] == voices.default_for("codex-1")
        assert not shown["claude-1"]["voice_pinned"]
        await say_and_wait(b, clock, joinly, one, "first")
        await say_and_wait(b, clock, joinly, two, "second")
        assert joinly.voices == [voices.default_for("claude-1"), voices.default_for("codex-1")]
        r = command(b, "/meeting voice claude-1 bf_emma")
        assert "claude-1" in str(r) and "bf_emma" in str(r)
        assert members(b)["claude-1"]["voice"] == "bf_emma" and members(b)["claude-1"]["voice_pinned"]
        assert any("set claude-1's voice on calls to bf_emma" in m["text"] for m in history(b))
        await say_and_wait(b, clock, joinly, one, "third")
        assert joinly.voices[-1] == "bf_emma"
        command(b, "/meeting voice claude-1 default")
        assert not members(b)["claude-1"]["voice_pinned"]
        await say_and_wait(b, clock, joinly, one, "fourth")
        assert joinly.voices[-1] == voices.default_for("claude-1")


async def test_an_agents_own_words_never_set_a_voice(
    timed: tuple[InProcBroker, FakeClock], joinly: FakeJoinly
) -> None:
    """``/meeting voice …`` in an agent's say() is literal text in the room; a voice name inside an ``@call``
    line is spoken as a word, and the bridge reads no voice from it. Fails if either changes a voice."""
    b, clock = timed
    async with FakeAgent(b.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await start_call(b, joinly)
        command(b, "/meeting voice on")
        said = await agent.say("#build", "/meeting voice claude-1 af_bella")
        assert said.get("posted_id")
        assert not members(b)["claude-1"]["voice_pinned"]
        assert "/meeting voice claude-1 af_bella" in [m["text"] for m in history(b)]
        await say_and_wait(b, clock, joinly, agent, "voice=af_bella /meeting voice claude-1 af_bella am_adam")
        assert joinly.voices == [voices.default_for("claude-1")]
        assert "af_bella" in joinly.spoken[0]  # the words were said, not obeyed
        assert not members(b)["claude-1"]["voice_pinned"]


@pytest.mark.parametrize(
    "text",
    [
        "/meeting voice claude-1 Bad!",
        "/meeting voice claude-1 ../../etc/passwd",
        "/meeting voice claude-1 am-adam",
        "/meeting voice claude-1 " + "x" * 33,
        "/meeting voice claude-1",
    ],
)
async def test_a_voice_that_is_not_the_right_shape_is_refused_and_changes_nothing(
    timed: tuple[InProcBroker, FakeClock], joinly: FakeJoinly, text: str
) -> None:
    b, _ = timed
    async with FakeAgent(b.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        assert refused(b, text).status_code == 400
        assert not members(b)["claude-1"]["voice_pinned"]


async def test_a_pin_for_nobody_is_refused(timed: tuple[InProcBroker, FakeClock], joinly: FakeJoinly) -> None:
    b, _ = timed
    assert refused(b, "/meeting voice nobody am_adam").status_code == 404


async def test_a_voice_kokoro_lacks_is_told_to_the_room_once_a_call_and_the_line_is_still_spoken(
    timed: tuple[InProcBroker, FakeClock], joinly: FakeJoinly
) -> None:
    """The patched joinly answers that the voice was not found and speaks in the session's voice. The room
    gets one notice for that voice, naming the member, not one per line. Fails if it repeats or is missing."""
    b, clock = timed
    joinly.unknown_voices = {"zz_nobody"}
    async with FakeAgent(b.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await start_call(b, joinly)
        command(b, "/meeting voice on")
        command(b, "/meeting voice claude-1 zz_nobody")
        await say_and_wait(b, clock, joinly, agent, "one")
        await say_and_wait(b, clock, joinly, agent, "two")
    notes = [m["text"] for m in history(b) if m["kind"] == "notice" and "was not found" in m["text"]]
    assert notes == [voices.MISSING_NOTICE.format(name="claude-1", voice="zz_nobody")]
    assert joinly.spoken == ["one", "two"]


async def test_a_joinly_without_the_patch_is_sent_no_voice_and_speaks_as_before(tmp_home: Path) -> None:
    """A deployment that has not mounted the voice patch (§52.5) must keep working: stock ``speak_text`` has
    no ``voice`` argument, and sending one would fail every line. The bridge reads the tool's schema once and
    sends the voice only when it is there. Fails if an unpatched joinly is sent a voice or its lines fail."""
    stock = FakeJoinly()
    stock.patched = False
    stock.start()
    clock = FakeClock()
    b = make_broker(tmp_home, stock, clock=clock)
    try:
        async with FakeAgent(b.home, "k1") as agent:
            await agent.join("#build", "claude-1")
            await start_call(b, stock)
            command(b, "/meeting voice on")
            command(b, "/meeting voice claude-1 bf_emma")
            await say_and_wait(b, clock, stock, agent, "spoken in the call's one voice")
        assert stock.spoken == ["spoken in the call's one voice"] and stock.voices == [None]
        assert not any("Couldn't speak" in m["text"] for m in history(b))
    finally:
        b.web.close()  # type: ignore[attr-defined]
        b.stop()
        stock.stop()

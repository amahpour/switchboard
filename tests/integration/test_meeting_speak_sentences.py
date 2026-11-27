"""An agent's ``@call`` line reaches joinly a sentence at a time (DESIGN.md §52.2, #761).

joinly makes the whole text of one ``speak_text`` into speech before it plays any of it. These tests
use the fake joinly, which takes ``synth_s_per_char`` seconds to make each call's text, with the
broker's own bot and its turn rules.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FakeClock, InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly
from test_meeting_voice import advance_voice, call_state, make_broker, speech_finished, start_call
from test_meetings import command, notices, wait_until


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


LINE = "@call Yes, the build is green. Two tests were slow, but they passed. I put the details in the room."
SENTENCES = [
    "Yes, the build is green.",
    "Two tests were slow, but they passed.",
    "I put the details in the room.",
]


async def test_the_first_sentence_starts_before_the_whole_line_is_made(
    timed: tuple[InProcBroker, FakeClock], joinly: FakeJoinly
) -> None:
    """Three sentences are three calls to joinly, in order, and the first starts playing after the time it
    takes to make that sentence, not the whole line. On main the line is one call and its start waits for
    all of it. Fails if the line is one speak_text, or the first sentence waits on the others.

    The time before the first sound is the fake's model of it: ``synth_s_per_char`` for each character of
    the first call's text. It was measured on the wall clock (``started[0] - arrived[0]``), but that is
    only the fake's own sleep plus how late its thread woke: the broker sets nothing in it but the text.
    On a busy CI runner the thread woke 0.115 s late and the 0.09 s margin failed. The model is checked
    instead, and the wall clock is printed."""
    broker, _ = timed
    joinly.synth_s_per_char = 0.004
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await start_call(broker, joinly)
        command(broker, "/meeting voice on")
        await agent.say("#build", LINE)
        await wait_until(lambda: len(joinly.started) == 3, timeout=10)
        await wait_until(lambda: speech_finished(broker))
    assert joinly.spoken == SENTENCES
    first = joinly.synth_s_per_char * len(joinly.received[0])  # made before the first sound
    whole = joinly.synth_s_per_char * len(" ".join(SENTENCES))
    waited = joinly.started[0] - joinly.arrived[0]
    print(f"\nfirst sentence made in {first:.2f} s (waited {waited:.2f} s); the whole line: {whole:.2f} s")
    assert first < whole / 2


async def test_sentences_are_one_turn_for_the_pacing_rules(
    timed: tuple[InProcBroker, FakeClock], joinly: FakeJoinly
) -> None:
    """While the first sentence plays, one more line may wait and the third is refused: the sentences of
    one answer do not count as separate lines. The waiting line is spoken after the first line's last
    sentence. Fails if each sentence takes a place in the queue, or the next line cuts in."""
    broker, clock = timed
    joinly.hold_speech = True
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await start_call(broker, joinly)
        command(broker, "/meeting voice on")
        await agent.say("#build", LINE)
        await wait_until(lambda: joinly.spoken == SENTENCES[:1])
        await agent.say("#build", "@call next line")
        await agent.say("#build", "@call one too many")
        assert notices(broker).count("already waiting") == 1
        joinly.release_speech()
        await wait_until(lambda: joinly.spoken == SENTENCES)
        await wait_until(lambda: speech_finished(broker))
        advance_voice(broker, clock, 30)
        await wait_until(lambda: joinly.spoken == [*SENTENCES, "next line"])


async def test_voice_off_drops_the_sentences_not_yet_spoken(
    timed: tuple[InProcBroker, FakeClock], joinly: FakeJoinly
) -> None:
    """Voice goes off while the first sentence plays: no later sentence reaches joinly. Fails if the
    rest of the line is spoken after a person turned voice off."""
    broker, _ = timed
    joinly.hold_speech = True
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await start_call(broker, joinly)
        command(broker, "/meeting voice on")
        await agent.say("#build", LINE)
        await wait_until(lambda: joinly.spoken == SENTENCES[:1])
        command(broker, "/meeting voice off")
        joinly.release_speech()
        await wait_until(lambda: speech_finished(broker))
    assert joinly.spoken == SENTENCES[:1]


async def test_leaving_drops_the_sentences_not_yet_spoken(
    timed: tuple[InProcBroker, FakeClock], joinly: FakeJoinly
) -> None:
    """The call ends while the first sentence plays: the rest never reach joinly. Fails if the worker keeps
    speaking a line into a call that is over."""
    broker, _ = timed
    joinly.hold_speech = True
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await start_call(broker, joinly)
        command(broker, "/meeting voice on")
        await agent.say("#build", LINE)
        await wait_until(lambda: joinly.spoken == SENTENCES[:1])
        command(broker, "/meeting leave")
        joinly.release_speech()
        await wait_until(lambda: call_state(broker) is None)
    assert joinly.spoken == SENTENCES[:1]

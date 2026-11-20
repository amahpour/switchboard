"""The call bar names the agent whose ``@call`` line is being spoken (#913, DESIGN.md §52.13).

The broker is the real one and joinly is ``FakeJoinly``, whose ``speak_text`` is held in the middle of a
line until the test releases it, and then finishes, is talked over or fails. The room's settings frame is
what the page draws the bar from: these tests read it from the REST route and record every frame the hub
fans out, so a state that is right when read but never sent fails as surely as a wrong one. Nothing waits
on a sleep: a test waits for the event it expects.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly
from test_meeting_voice import call_state, make_broker, notice_texts, speech_finished, start_call
from test_meetings import command, wait_until


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


def record_frames(b: InProcBroker) -> list[dict[str, Any] | None]:
    """Every ``settings.meeting`` the hub sends to #build's pages from now on, in order."""
    frames: list[dict[str, Any] | None] = []
    hub = b.state.hub
    real = hub.room_settings

    def recording(room: str, settings: dict[str, Any]) -> int:
        if room == "#build":
            frames.append(settings.get("meeting"))
        return real(room, settings)

    b.on_loop(setattr, hub, "room_settings", recording)
    return frames


def speaking(b: InProcBroker) -> Any:
    return (call_state(b) or {}).get("speaking")


def spoken_names(frames: list[dict[str, Any] | None]) -> list[Any]:
    """Who each frame said was speaking, with repeats of the same answer folded into one. Frames before the
    first that names anyone are left out: other things in the room (a post, a member's state) may send one."""
    out: list[Any] = []
    for frame in frames:
        name = (frame or {}).get("speaking")
        if (out or name is not None) and (not out or out[-1] != name):
            out.append(name)
    return out


async def speak_held_line(
    b: InProcBroker, joinly: FakeJoinly, agent: FakeAgent
) -> list[dict[str, Any] | None]:
    """Start a call with voice on, then have claude-1 say a line that joinly holds mid-speech."""
    joinly.hold_speech = True
    await agent.join("#build", "claude-1")
    await start_call(b, joinly)
    command(b, "/meeting voice on")
    frames = record_frames(b)
    assert (await agent.say("#build", "@call the build is green")).get("posted_id")
    await wait_until(lambda: joinly.spoken == ["the build is green"])
    await wait_until(lambda: speaking(b) == "claude-1")
    return frames


async def test_the_bar_names_the_agent_while_its_line_is_spoken_and_clears_when_it_ends(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """While joinly's ``speak_text`` is saying claude-1's line, the frame the page gets says
    ``speaking: claude-1``; once it returns, a frame without it goes out. Fails if the bar is never told,
    is told only when the page reloads, or keeps saying it after the line is over."""
    async with FakeAgent(broker.home, "k1") as agent:
        frames = await speak_held_line(broker, joinly, agent)
        assert call_state(broker) == {
            "state": "live",
            "platform": "meet",
            "voice": True,
            "speaking": "claude-1",
        }
        await wait_until(lambda: spoken_names(frames)[-1:] == ["claude-1"])
        joinly.release_speech()
        await wait_until(lambda: speaking(broker) is None)
        await wait_until(lambda: spoken_names(frames) == ["claude-1", None])
    assert "speaking" not in (call_state(broker) or {})


@pytest.mark.parametrize(
    ("how", "notice"),
    [("interrupt_next", "spoke over the line"), ("fail_speech", "Couldn't speak in the call")],
)
async def test_the_bar_clears_when_the_line_is_talked_over_or_fails(
    broker: InProcBroker, joinly: FakeJoinly, how: str, notice: str
) -> None:
    """Someone speaking over the bot, or ``speak_text`` failing, ends the line early: the bar stops naming
    the agent then too, and the room hears why. Fails if only a finished line clears the bar, so an
    interrupted or failed one leaves a stale 'speaking'."""
    async with FakeAgent(broker.home, "k1") as agent:
        frames = await speak_held_line(broker, joinly, agent)
        setattr(joinly, how, True)
        joinly.release_speech()
        await wait_until(lambda: any(notice in t for t in notice_texts(broker)))
        await wait_until(lambda: speaking(broker) is None)
        await wait_until(lambda: spoken_names(frames) == ["claude-1", None])


async def test_a_line_waiting_its_turn_is_not_shown_as_speaking(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """While claude-1's line is spoken, codex-1's line waits for the slot: the bar names claude-1 only, and
    names codex-1 only once its own line is being said. Fails if a queued line counts as speaking."""
    async with FakeAgent(broker.home, "k1") as claude, FakeAgent(broker.home, "k2") as codex:
        await codex.join("#build", "codex-1")
        frames = await speak_held_line(broker, joinly, claude)
        assert (await codex.say("#build", "@call and the deploy is next")).get("posted_id")
        room_id = broker.state.store.get_room("#build").id
        await wait_until(
            lambda: broker.on_loop(lambda: broker.state.meetings._live[room_id].turns.pending is not None)
        )
        assert speaking(broker) == "claude-1"
        assert "codex-1" not in spoken_names(frames)
        joinly.release_speech()
        await wait_until(lambda: len(joinly.spoken) == 2)
        await wait_until(lambda: speech_finished(broker) and speaking(broker) is None)
    assert spoken_names(frames) == ["claude-1", None, "codex-1", None]


async def test_leaving_the_call_mid_line_leaves_no_speaking_behind(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """A call that ends while a line is held ends with it: the bar goes, and the next call starts with
    nobody speaking. Fails if the name outlives the call that said it."""
    async with FakeAgent(broker.home, "k1") as agent:
        await speak_held_line(broker, joinly, agent)
        command(broker, "/meeting leave")
        await wait_until(lambda: call_state(broker) is None)
        joinly.release_speech()
        await start_call(broker, joinly)
        assert "speaking" not in (call_state(broker) or {})

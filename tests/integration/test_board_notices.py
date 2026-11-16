"""A burst of board changes is one room notice, posted when it ends (§56.3, #788).

An agent that draws one shape at a time used to post a line per shape. Now each actor's burst is one
"X changed the board (N edits)" line; a quiet gap or another actor's write closes it, and shutdown
posts what is still open. The board's own fan-out to browsers is untouched."""

from __future__ import annotations

import pytest
from conftest import FakeClock, InProcBroker
from fakes.fake_agent import FakeAgent
from test_board_tool import broker, q  # noqa: F401


def notes(b: InProcBroker) -> list[str]:
    return [r["text"] for r in q(b, "SELECT text FROM messages") if "changed the board" in r["text"]]


def flush(b: InProcBroker) -> None:
    b.on_loop(b.state.room_board.flush_due)


async def draw(agent: FakeAgent, clock: FakeClock, n: int) -> None:
    for _ in range(n):
        clock.advance(3)  # past the board's refill, so none is rate limited
        made = await agent.call("board", room="#build", action="note", text="x")
        assert made["changed"] is True, made


async def test_thirty_writes_by_one_actor_are_one_notice_that_reads_thirty(
    broker: InProcBroker,  # noqa: F811
    clock: FakeClock,
):
    """Fails if each write still posts its own line, or the count is lost."""
    async with FakeAgent(broker.home, "drawer") as drawer:
        await drawer.join("#build", "claude-1")
        await draw(drawer, clock, 30)
        assert notes(broker) == []  # the burst is still open
        clock.advance(130)
        flush(broker)
    assert notes(broker) == ["claude-1 changed the board (30 edits)"]


async def test_another_actors_write_closes_the_burst_and_starts_its_own(
    broker: InProcBroker,  # noqa: F811
    clock: FakeClock,
):
    """Fails if two actors' edits are counted into one line."""
    async with FakeAgent(broker.home, "a") as a, FakeAgent(broker.home, "b") as b:
        await a.join("#build", "claude-1")
        await b.join("#build", "codex-1")
        await draw(a, clock, 3)
        await draw(b, clock, 2)
        assert notes(broker) == ["claude-1 changed the board (3 edits)"]
        await draw(a, clock, 1)
        assert notes(broker) == [
            "claude-1 changed the board (3 edits)",
            "codex-1 changed the board (2 edits)",
        ]
        clock.advance(130)
        flush(broker)
    assert notes(broker)[-1] == "claude-1 changed the board"


async def test_a_burst_that_ends_in_silence_is_flushed_by_its_timer(
    broker: InProcBroker,  # noqa: F811
    clock: FakeClock,
):
    """The timer, not another write, posts it. Fails if nothing is armed: the notice would never come."""
    async with FakeAgent(broker.home, "drawer") as drawer:
        await drawer.join("#build", "claude-1")
        await draw(drawer, clock, 2)
        timers = broker.on_loop(lambda: len(broker.state.room_board._timers))
        assert timers == 1
        clock.advance(130)
        broker.on_loop(lambda: broker.state.room_board._wake(next(iter(broker.state.room_board._bursts))))
    assert notes(broker) == ["claude-1 changed the board (2 edits)"]
    assert broker.on_loop(lambda: len(broker.state.room_board._timers)) == 0


async def test_shutdown_posts_the_open_burst_and_leaves_no_timer(
    broker: InProcBroker,  # noqa: F811
    clock: FakeClock,
):
    """Fails if a stop loses the last burst or leaves a timer running."""
    async with FakeAgent(broker.home, "drawer") as drawer:
        await drawer.join("#build", "claude-1")
        await draw(drawer, clock, 4)
        broker.on_loop(broker.state.room_board.close)
    board = broker.state.room_board
    assert notes(broker) == ["claude-1 changed the board (4 edits)"]
    assert board._timers == {} and board._bursts == {}


@pytest.mark.parametrize("gap", [119, 120])
def test_the_gap_is_what_closes_a_burst(gap: float):
    """Pure policy with the clock as a number: inside the gap extends, at the gap starts anew."""
    from switchboard.delivery import rules

    closed, cur = rules.board_burst_step(("a", 2, 100.0), "a", 100.0 + gap)
    assert (closed is None) == (gap < 120) and cur[1] == (3 if gap < 120 else 1)

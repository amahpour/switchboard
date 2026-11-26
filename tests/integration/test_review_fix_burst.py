"""One room line for a burst of fixes, through the real ``review`` tool and broker (#984, DESIGN.md §37.20).

claude-1 is the author and codex-1 the reviewer. The author marks findings fixed one after another: the
cards move at once, and the room hears of them once, mentioning the reviewer once, when no fix has come for
the quiet wait. The clock is a ``FakeClock``; the tests wait for the runner's tick, never for a time.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard import reviews
from switchboard.config import Config

PR = "https://github.com/example/shop/pull/7"
FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)


def rows(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def fix_lines(b: InProcBroker) -> list[sqlite3.Row]:
    return rows(b, "SELECT id, text, mentions FROM messages WHERE kind='chat' AND text LIKE '%fixed in%'")


async def until(what: Any) -> Any:
    """Wait for the runner's next tick to have done ``what`` (a callable that returns something true)."""
    for _ in range(200):
        got = what()
        if got:
            return got
        await asyncio.sleep(0.05)
    raise AssertionError("the broker never did it")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def broker(tmp_home: Path, clock: FakeClock):
    b = InProcBroker(tmp_home, FAST, clock=clock).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    yield b
    web.close()
    b.stop()


async def board_with_findings(author: FakeAgent, reviewer: FakeAgent, n: int) -> None:
    """The reviewer opens the board and raises ``n`` findings; the author concedes each."""
    await author.join("#build", "claude-1")
    await reviewer.join("#build", "codex-1")
    await reviewer.call("review", room="#build", action="open", url=PR, head="abc1234")
    for i in range(1, n + 1):
        await reviewer.call("review", room="#build", action="raise", title=f"problem {i}")
        await author.call("review", room="#build", action="concede", item=f"F{i}")


async def fix(author: FakeAgent, item: str, commit: str = "def5678") -> dict[str, Any]:
    return await author.call("review", room="#build", action="fix", item=item, commit=commit, proof="test_it")


async def test_three_fixes_close_together_are_one_line_and_one_wake(
    broker: InProcBroker, clock: FakeClock
) -> None:
    """F1, F2 and F3 marked fixed 10 s apart: the cards are fixed at once, nothing is posted for 29 s after
    the last, then one message names all three and mentions codex-1 once, delivered to it once. Fails if
    each fix posts its own line (three wakes), if the line is posted before the quiet wait, or if the card
    waits for the line."""
    async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
        await board_with_findings(author, reviewer, 3)
        for item in ("F1", "F2", "F3"):
            r = await fix(author, item)
            assert f"{item} [fixed]" in r["board"]  # the card moved at once
            shown = await reviewer.call("review", room="#build", action="show")
            assert f"{item} [fixed]" in shown["board"]
            clock.advance(10)
        clock.advance(reviews.FIX_QUIET_S - 10 - 1)
        assert broker.on_loop(broker.state.boards.flush_held) == 0  # the tick's own work: nothing is due
        assert fix_lines(broker) == []
        clock.advance(1)
        [line] = await until(lambda: fix_lines(broker))
        assert line["text"] == (
            "Review board: F1, F2 and F3 fixed in def5678. @codex-1: check the fixes."
            ' review(action="show") shows the board.'
        )
        assert json.loads(line["mentions"]) == ["codex-1"] and line["text"].count("@") == 1
        got = rows(
            broker,
            "SELECT m.screen_name FROM deliveries d JOIN memberships m ON m.id=d.membership_id"
            " WHERE d.message_id=?",
            line["id"],
        )
        assert [r["screen_name"] for r in got] == ["codex-1"]
        assert broker.on_loop(broker.state.boards.flush_held) == 0  # posted once, not held again
        assert len(fix_lines(broker)) == 1
        assert rows(broker, "SELECT held_fixes FROM reviews")[0]["held_fixes"] == ""


async def test_a_single_fix_goes_out_after_the_wait_worded_as_before(
    broker: InProcBroker, clock: FakeClock
) -> None:
    """One fix is posted 30 s after it was marked, as "F1 fixed in <sha>. @codex-1: check the fix." Fails if
    a lone fix is held longer or worded differently."""
    async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
        await board_with_findings(author, reviewer, 1)
        await fix(author, "F1")
        assert fix_lines(broker) == []
        clock.advance(reviews.FIX_QUIET_S)
        [line] = await until(lambda: fix_lines(broker))
        assert line["text"] == (
            "Review board: F1 fixed in def5678. @codex-1: check the fix."
            ' review(action="show") shows the board.'
        )


async def test_other_board_lines_are_not_held(broker: InProcBroker) -> None:
    """A raise and a contest reach the other side at once, with no tick and no time passing. Fails if the
    hold swallows lines it was not asked to."""
    async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
        await author.join("#build", "claude-1")
        await reviewer.join("#build", "codex-1")
        await reviewer.call("review", room="#build", action="open", url=PR, head="abc1234")
        await reviewer.call("review", room="#build", action="raise", title="problem 1")
        await author.call("review", room="#build", action="contest", item="F1", reason="it is fine")
        texts = [r["text"] for r in rows(broker, "SELECT text FROM messages WHERE kind='chat' ORDER BY id")]
        assert len(texts) == 2 and "F1 raised" in texts[0]
        assert "F1 contested" in texts[1] and "@codex-1" in texts[1]


async def test_a_fix_the_reviewer_confirms_before_the_line_goes_out_is_not_in_it(
    broker: InProcBroker, clock: FakeClock
) -> None:
    """F1 and F2 are fixed, the reviewer confirms F1 within the wait: the line names F2 alone. Fails if the
    line asks for a check the reviewer already made."""
    async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
        await board_with_findings(author, reviewer, 2)
        await fix(author, "F1")
        await fix(author, "F2")
        await reviewer.call("review", room="#build", action="confirm", item="F1")
        clock.advance(reviews.FIX_QUIET_S)
        [line] = await until(lambda: fix_lines(broker))
        assert line["text"].startswith("Review board: F2 fixed in def5678. @codex-1: check the fix.")


async def test_a_held_line_survives_a_restart_and_is_posted_after_it_starts(
    tmp_home: Path, clock: FakeClock
) -> None:
    """Two fixes are held when the broker stops. The line is not posted by the stop, the hold is in the
    database, and the broker that starts on that home posts one line for both once the wait is over, and
    not before. Fails if a restart loses the hold, posts it early, or posts it twice."""
    b = InProcBroker(tmp_home, FAST, clock=clock).start()
    try:
        web = b.web_client()
        assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
        web.close()
        async with FakeAgent(b.home, "k1") as author, FakeAgent(b.home, "k2") as reviewer:
            await board_with_findings(author, reviewer, 2)
            await fix(author, "F1")
            clock.advance(5)
            await fix(author, "F2")
        b.stop()
        assert fix_lines(b) == []
        held = json.loads(rows(b, "SELECT held_fixes FROM reviews")[0]["held_fixes"])
        assert [g["fixes"] for g in held] == [["F1", "F2"]]
        b = InProcBroker(tmp_home, FAST, clock=clock).start()
        assert b.on_loop(b.state.boards.flush_held) == 0  # started, and still inside the wait
        assert fix_lines(b) == []
        clock.advance(reviews.FIX_QUIET_S)
        [line] = await until(lambda: fix_lines(b))
        assert "F1 and F2 fixed in def5678. @codex-1: check the fixes." in line["text"]
        assert b.on_loop(b.state.boards.flush_held) == 0
        assert len(fix_lines(b)) == 1
    finally:
        b.stop()

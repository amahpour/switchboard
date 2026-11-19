"""Who a board's move wakes, through the real ``review`` tool and broker (#884, DESIGN.md §37.17).

Three scripted agents in one room: claude-1 opened the board for its pull request, codex-1 reviews it,
and grok-1 is in the room for another one. codex-1's raises reach claude-1 alone, as one message when
they come together, and claude-1's moves on the board answer them, though it never says a word.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from conftest import FakeClock, InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard.config import Config

PR = "https://github.com/example/shop/pull/7"


def rows(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def board_lines(b: InProcBroker) -> list[sqlite3.Row]:
    return rows(
        b, "SELECT id, text, mentions FROM messages WHERE kind='chat' AND sender_name='codex-1' ORDER BY id"
    )


def delivery(b: InProcBroker, name: str, msg_id: int) -> str | None:
    got = rows(
        b,
        "SELECT d.state FROM deliveries d JOIN memberships m ON m.id=d.membership_id"
        " WHERE m.screen_name=? AND d.message_id=?",
        name,
        msg_id,
    )
    return got[0]["state"] if got else None


def start(tmp_home: Path, cfg: Config, clock: Any = None) -> InProcBroker:
    b = InProcBroker(tmp_home, cfg, clock=clock).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    web.close()
    return b


async def test_a_raise_reaches_the_author_alone_and_its_board_moves_answer_it(tmp_home: Path) -> None:
    """The raise names claude-1, which opened the board, and nobody else: grok-1 gets no delivery of it.
    claude-1 reads it and contests it on the board, and the line is handled, as a say would have made
    it. Fails on main, where the raise named grok-1 too and only a say() or pass() answered it."""
    b = start(tmp_home, Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0))
    try:
        async with (
            FakeAgent(b.home, "author") as author,
            FakeAgent(b.home, "reviewer") as reviewer,
            FakeAgent(b.home, "bystander") as bystander,
        ):
            await author.join("#build", "claude-1")
            await reviewer.join("#build", "codex-1")
            await bystander.join("#build", "grok-1")
            await author.call("review", room="#build", action="open", url=PR)
            await reviewer.call("review", room="#build", action="raise", title="off by one", where="a.py:3")
            [line] = board_lines(b)
            assert line["text"].startswith(
                "Review board: F1 raised: off by one. @claude-1: concede or contest it."
            )
            assert line["mentions"] == '["claude-1"]'
            assert delivery(b, "grok-1", line["id"]) is None
            got = await author.call("read", room="#build")
            assert f"id={line['id']} " in got["text"]
            await author.call("review", room="#build", action="contest", item="F1", reason="it starts at 1")
            assert delivery(b, "claude-1", line["id"]) == "handled"
            # the contest is codex-1's to answer, and it reached codex-1 alone too
            [contest] = rows(
                b, "SELECT id, mentions FROM messages WHERE sender_name='claude-1' AND kind='chat'"
            )
            assert contest["mentions"] == '["codex-1"]' and delivery(b, "grok-1", contest["id"]) is None
    finally:
        b.stop()


async def test_raises_made_together_go_as_one_message_after_the_quiet_period(tmp_home: Path) -> None:
    """Three raises at the same moment by the broker's clock: nothing is posted until the room's quiet
    period has passed on that clock, then one message names all three. A say by the reviewer goes after
    the lines it made, not before them. Fails if each raise posted at once (eleven lines in #884)."""
    clock = FakeClock()
    b = start(tmp_home, Config(human_name="alice"), clock=clock)
    try:
        async with FakeAgent(b.home, "author") as author, FakeAgent(b.home, "reviewer") as reviewer:
            await author.join("#build", "claude-1")
            await reviewer.join("#build", "codex-1")
            await author.call("review", room="#build", action="open", url=PR)
            for n in (1, 2, 3):
                await reviewer.call("review", room="#build", action="raise", title=f"finding {n}")
            assert board_lines(b) == []
            clock.advance(3.0)
            b.on_loop(b.state.boards.flush_due)
            [line] = board_lines(b)
            assert line["text"].startswith(
                "Review board: F1 to F3 raised. @claude-1: concede or contest each."
            )
            await reviewer.call("review", room="#build", action="raise", title="finding 4")
            await reviewer.call("say", room="#build", text="That is all from me.")
            texts = [r["text"] for r in board_lines(b)]
            assert (
                texts[1].startswith("Review board: F4 raised: finding 4.")
                and texts[2] == "That is all from me."
            )
    finally:
        b.stop()


async def test_a_held_line_is_posted_when_the_broker_stops(tmp_home: Path) -> None:
    """A restart inside the few seconds a burst waits posts it rather than lose the raise's wake."""
    clock = FakeClock()
    b = start(tmp_home, Config(human_name="alice"), clock=clock)
    try:
        async with FakeAgent(b.home, "author") as author, FakeAgent(b.home, "reviewer") as reviewer:
            await author.join("#build", "claude-1")
            await reviewer.join("#build", "codex-1")
            await author.call("review", room="#build", action="open", url=PR)
            await reviewer.call("review", room="#build", action="raise", title="off by one")
            assert board_lines(b) == []
    finally:
        b.stop()
    [line] = board_lines(b)
    assert "F1 raised: off by one" in line["text"]

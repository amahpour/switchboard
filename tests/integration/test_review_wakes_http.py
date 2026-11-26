"""Who a board's move wakes, through the real ``review`` tool and broker (#884, DESIGN.md §37.17).

Three scripted agents in one room: claude-1 opened the board for its pull request, codex-1 reviews it,
and grok-1 is in the room for another one. codex-1's raises reach claude-1 alone, eleven of them as one
wake, and claude-1's moves on the board answer them, though it never says a word.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from conftest import InProcBroker
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


async def test_eleven_quick_raises_reach_the_author_as_one_wake(tmp_home: Path) -> None:
    """Eleven raises in a row through the real tool, with the broker's own delivery settings: each is
    its own line to claude-1 alone, and claude-1, waiting in wait(), is answered once. Its next read()
    has the rest. One wake for the eleven, from the engine's own batching, and nothing for the agent
    that isn't on the pull request."""
    b = start(tmp_home, Config(human_name="alice"))
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
            # its wait() may open before the raises or among them: either way it is answered once
            waiting = asyncio.create_task(author.call("wait", room="#build", timeout_s=30))
            for n in range(1, 12):
                await reviewer.call("review", room="#build", action="raise", title=f"finding {n}")
            got = await waiting
            lines = board_lines(b)
            assert len(lines) == 11 and all(x["mentions"] == '["claude-1"]' for x in lines)
            assert f"id={lines[0]['id']} " in got["text"]
            rest = await author.call("read", room="#build")
            seen = got["text"] + rest["text"]
            assert all(f"id={x['id']} " in seen for x in lines)
            wakes = rows(
                b,
                "SELECT COUNT(*) AS n FROM batches b JOIN memberships m ON m.id=b.membership_id"
                " WHERE m.screen_name=? AND b.kind='wake'",
                "claude-1",
            )
            assert wakes[0]["n"] == 1
            assert all(delivery(b, "grok-1", x["id"]) is None for x in lines)
    finally:
        b.stop()


async def test_a_raise_on_a_board_with_no_side_goes_to_the_agent_claiming_its_pull_request(
    tmp_home: Path,
) -> None:
    """A reviewer opens the board and raises, so the board records no author. claude-1 holds a live
    claim on the pull request: the raise names it alone and grok-1 gets no delivery (#969). With no
    claim, or once the claim is released, it names every other agent. Fails on main, where the
    raise named grok-1 as well."""
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
            await reviewer.call("review", room="#build", action="open", url=PR)
            await reviewer.call("review", room="#build", action="raise", title="no claim", where="a.py:1")
            [bare] = board_lines(b)
            assert bare["mentions"] == '["claude-1", "grok-1"]'
            await author.call("claim", room="#build", key=PR, ttl=300)
            await reviewer.call("review", room="#build", action="raise", title="claimed", where="a.py:2")
            claimed = board_lines(b)[-1]
            assert claimed["text"].startswith("Review board: F2 raised: claimed. @claude-1: concede")
            assert claimed["mentions"] == '["claude-1"]'
            assert delivery(b, "grok-1", claimed["id"]) is None
            await author.call("claim", room="#build", key=PR, ttl=0)
            await reviewer.call("review", room="#build", action="raise", title="released", where="a.py:3")
            assert board_lines(b)[-1]["mentions"] == '["claude-1", "grok-1"]'
    finally:
        b.stop()

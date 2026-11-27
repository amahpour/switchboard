"""A review board's ready line, through the real ``review`` tool and broker (#985, DESIGN.md §37.21).

lead-1 is the room's coordinator, claude-1 the author, codex-1 the reviewer. Ready at a commit means: the
coordinator reported CI passing at it, every card is settled, and the reviewers gave a verdict at it. The line
is information only, hidden in a room without a coordinator, and said once to the coordinator when it becomes
true. Every test names the way it would fail.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard.config import Config

PR = "https://github.com/example/shop/pull/7"
FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0, rate_limit_s=0.0)
OPENED = "abc1234"
FIX = "def5678"
FIX_FULL = FIX + "9" * 33  # the same commit, written out in full (a report reads it off the forge)
SHORT = FIX_FULL[:12]  # what the header and the line call it once a report told the full one
NEWER = "1111111"
NEWER_FULL = NEWER + "2" * 33
SHORT2 = NEWER_FULL[:12]


@pytest.fixture
def broker(tmp_home: Path) -> Iterator[InProcBroker]:
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web  # type: ignore[attr-defined]
    yield b
    web.close()
    b.stop()


def person(b: InProcBroker, text: str) -> None:
    r = b.web.post(  # type: ignore[attr-defined]
        "/api/rooms/build/command", json={"text": text}, headers=b.write_headers()
    )
    assert r.status_code == 200, r.text


def rows(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def ready_lines(b: InProcBroker) -> list[sqlite3.Row]:
    return rows(b, "SELECT id, text, mentions FROM messages WHERE kind='chat' AND text LIKE '%is ready at%'")


def page_line(b: InProcBroker) -> str:
    """What the page is sent for the board's line (its ``ready`` field)."""
    [board] = b.on_loop(b.state.boards.boards, b.on_loop(b.state.store.get_room, "#build"))
    return board["ready"]


class Room:
    """lead-1 (coordinator), claude-1 (author), codex-1 (reviewer), joined and with a board at ``OPENED``."""

    def __init__(self, broker: InProcBroker, lead: FakeAgent, author: FakeAgent, reviewer: FakeAgent) -> None:
        self.broker, self.lead, self.author, self.reviewer = broker, lead, author, reviewer

    async def review(self, who: FakeAgent, **args: Any) -> dict[str, Any]:
        return await who.call("review", room="#build", **args)

    async def show(self) -> str:
        return str((await self.review(self.reviewer, action="show"))["board"])

    async def fixed_and_confirmed(self) -> None:
        """codex-1 raises F1, claude-1 concedes and fixes it at ``FIX``, codex-1 confirms: settled."""
        await self.review(self.reviewer, action="raise", title="off by one")
        await self.review(self.author, action="concede", item="F1")
        await self.review(self.author, action="fix", item="F1", commit=FIX, proof="test_it")
        await self.review(self.reviewer, action="confirm", item="F1")

    async def verdict(self, who: FakeAgent, head: str = FIX) -> dict[str, Any]:
        return await self.review(who, action="verdict", head=head)

    async def ci(self, who: FakeAgent, head: str = FIX_FULL, ci: str = "passing") -> dict[str, Any]:
        return await self.review(who, action="report", ci=ci, head=head)


async def room(broker: InProcBroker, *, coordinator: bool = True) -> Any:
    async with (
        FakeAgent(broker.home, "k0") as lead,
        FakeAgent(broker.home, "k1") as author,
        FakeAgent(broker.home, "k2") as reviewer,
    ):
        await lead.join("#build", "lead-1")
        await author.join("#build", "claude-1")
        await reviewer.join("#build", "codex-1")
        if coordinator:
            person(broker, "/coordinator lead-1 none")  # no action ticked: the ready line needs none
        r = Room(broker, lead, author, reviewer)
        await r.review(reviewer, action="open", url=PR, head=OPENED)
        yield r


@pytest.fixture
async def world(broker: InProcBroker) -> Any:
    async for r in room(broker):
        yield r


@pytest.fixture
async def bare(broker: InProcBroker) -> Any:
    async for r in room(broker, coordinator=False):
        yield r


async def test_ready_when_ci_cards_and_verdicts_all_hold_and_the_coordinator_is_told_once(
    world: Room,
) -> None:
    """The three parts at the board's latest commit read "Ready at <sha>", and the commit named in a 7
    character verdict and a 40 character report are one. The coordinator hears of it in one line addressed to
    it alone, the last verdict and a later repeat never say it again, and it is stored, so a restart does not
    either. Fails if the line says anything else, a short and a long hash do not match, the line goes to the
    room, or it is said twice."""
    await world.fixed_and_confirmed()
    assert "Not ready:" in await world.show()
    await world.ci(world.lead)
    assert f"Not ready: codex-1 has no verdict at {SHORT}" in await world.show()
    assert ready_lines(world.broker) == []
    await world.verdict(world.reviewer)
    assert f"\nReady at {SHORT}\n" in await world.show() + "\n"
    [line] = ready_lines(world.broker)
    assert line["text"].startswith(f"Review board for #7 is ready at {SHORT}. @lead-1:")
    assert json.loads(line["mentions"]) == ["lead-1"]
    got = rows(
        world.broker,
        "SELECT m.screen_name FROM deliveries d JOIN memberships m ON m.id=d.membership_id"
        " WHERE d.message_id=?",
        line["id"],
    )
    assert [r["screen_name"] for r in got] == ["lead-1"]
    await world.verdict(world.reviewer)  # its own verdict again, and the coordinator's report again
    await world.ci(world.lead)
    assert len(ready_lines(world.broker)) == 1
    sent = rows(world.broker, "SELECT data FROM events WHERE kind='review' AND data LIKE '%\"ready\"%'")
    assert len(sent) == 1 and json.loads(sent[0]["data"])["ready"] == FIX_FULL


@pytest.mark.parametrize("missing", ["ci", "cards", "verdict"])
async def test_each_part_missing_alone_is_named_and_the_others_are_not(world: Room, missing: str) -> None:
    """With two of the three parts there, the line names only the third: no CI report from the coordinator,
    a fix still waiting for its check, or no verdict. Fails if a part is not counted, is counted without
    being there, or its absence is not named."""
    await world.review(world.reviewer, action="raise", title="off by one")
    await world.review(world.author, action="concede", item="F1")
    await world.review(world.author, action="fix", item="F1", commit=FIX, proof="test_it")
    if missing != "cards":
        await world.review(world.reviewer, action="confirm", item="F1")
    if missing != "ci":
        await world.ci(world.lead)
    if missing != "verdict":
        await world.verdict(world.reviewer)
    shown = await world.show()
    want = {
        "ci": f"Not ready: CI not reported passing at {FIX} by lead-1",  # nothing but the fix told a commit
        "cards": "Not ready: F1 awaits its check",
        "verdict": f"Not ready: codex-1 has no verdict at {SHORT}",
    }[missing]
    assert f"\n{want}\n" in shown + "\n"
    assert ready_lines(world.broker) == []


async def test_another_agents_report_a_report_at_an_older_commit_and_a_failing_one_do_not_count(
    world: Room,
) -> None:
    """Only the coordinator's CI report at the latest commit, saying passing, counts: the author's report does
    not, nor the coordinator's at the commit before, nor its own newer "failing". Fails if any of them makes
    the board ready."""
    await world.fixed_and_confirmed()
    await world.verdict(world.reviewer)
    await world.ci(world.author)  # claude-1 is not the coordinator
    assert f"CI not reported passing at {SHORT} by lead-1" in await world.show()
    # passing, but at the commit before: that report is told too, so the board is read at that commit, where
    # nobody has given a verdict (the report counts as a CI report there, and nothing else)
    await world.ci(world.lead, head=OPENED + "0" * 33)
    older = await world.show()
    assert "Ready" not in older and "codex-1 has no verdict at abc123400000" in older
    await world.ci(world.lead, ci="failing")
    assert "CI not reported passing" in await world.show()
    assert ready_lines(world.broker) == []
    await world.ci(world.lead)
    assert f"Ready at {SHORT}" in await world.show()


async def test_a_new_commit_waits_for_a_new_report_and_new_verdicts(world: Room) -> None:
    """After the board is ready, a new head makes it not ready again: the old report and verdict were at
    another commit. The coordinator is told again, once, when the new commit is ready. Fails if an old report
    or verdict carries over, or the new commit is not told."""
    await world.fixed_and_confirmed()
    await world.ci(world.lead)
    await world.verdict(world.reviewer)
    assert len(ready_lines(world.broker)) == 1
    await world.review(world.reviewer, action="open", url=PR, head=NEWER)
    shown = await world.show()
    assert f"CI not reported passing at {NEWER} by lead-1" in shown
    assert f"codex-1 has no verdict at {NEWER}" in shown
    await world.ci(world.lead, head=NEWER_FULL)
    await world.verdict(world.reviewer, NEWER)
    assert f"Ready at {SHORT2}" in await world.show()
    assert [r["text"].split(". @")[0] for r in ready_lines(world.broker)] == [
        f"Review board for #7 is ready at {SHORT}",
        f"Review board for #7 is ready at {SHORT2}",
    ]


async def test_the_authors_side_cannot_give_a_verdict(world: Room) -> None:
    """claude-1 owns F1: its verdict is refused, says why, and is not stored, so it cannot make the board
    ready. A verdict needs a head, and a bad head is refused. Fails if the author can vouch for its own work,
    or a verdict without a commit is stored."""
    await world.fixed_and_confirmed()
    await world.ci(world.lead)
    refused = await world.verdict(world.author)
    assert not refused["ok"] and "author's side" in refused["error"]
    assert "no verdict" in await world.show()
    for args in ({"action": "verdict"}, {"action": "verdict", "head": "not-a-sha"}):
        bad = await world.review(world.reviewer, **args)
        assert not bad["ok"] and "head" in bad["error"]
    assert ready_lines(world.broker) == []
    assert rows(world.broker, "SELECT 1 FROM events WHERE data LIKE '%\"verdict\"%'") == []


async def test_a_verdict_is_shown_replaced_by_the_next_and_settles_no_finding(world: Room) -> None:
    """A verdict reads on the board as "Verdicts: codex-1 at <sha>", is a notice and wakes no one, is
    replaced by the same agent's next one, and leaves its open finding open. Fails if it settles a card,
    wakes an agent, or the older verdict stays beside the newer."""
    await world.review(world.reviewer, action="raise", title="off by one")
    await world.verdict(world.reviewer, OPENED)
    shown = await world.show()
    assert "Verdicts: codex-1 at abc1234" in shown and "F1 [raised]" in shown
    await world.verdict(world.reviewer, FIX)
    shown = await world.show()
    assert f"Verdicts: codex-1 at {FIX}" in shown and "Verdicts: codex-1 at abc1234" not in shown
    said = rows(world.broker, "SELECT text, kind FROM messages WHERE text LIKE '%reviewed%'")
    assert [r["kind"] for r in said] == ["notice", "notice"]
    assert (
        rows(
            world.broker,
            "SELECT 1 FROM deliveries d JOIN messages m ON m.id=d.message_id"
            " WHERE m.kind='notice' AND d.state='pending' AND m.text LIKE '%reviewed%'",
        )
        == []
    )


async def test_without_a_coordinator_there_is_no_line_and_no_message_and_naming_one_shows_it(
    bare: Room,
) -> None:
    """A room with no coordinator gets no line in `show` or on the page, and no message, though every part
    holds. Naming one shows the line, removing it hides it again. Fails if the line appears without a
    coordinator, a message is sent, or the page is not refreshed when one is named."""
    await bare.fixed_and_confirmed()
    await bare.verdict(bare.reviewer)
    await bare.ci(bare.lead)
    for text in (await bare.show(), page_line(bare.broker)):
        assert "Ready" not in text and "Not ready" not in text
    assert ready_lines(bare.broker) == []
    person(bare.broker, "/coordinator lead-1 none")
    assert f"Ready at {SHORT}" in await bare.show()
    assert page_line(bare.broker) == f"Ready at {SHORT}"
    person(bare.broker, "/coordinator off")
    assert "Ready" not in await bare.show() and page_line(bare.broker) == ""


async def test_a_reviewer_who_left_does_not_hold_the_board_back_and_is_named(world: Room) -> None:
    """codex-2 raised a finding that was dropped, then left with no verdict: the board is still ready on
    codex-1's verdict, and the line says "codex-2 left without a verdict". Fails if the leaver blocks it, or
    is not named."""
    async with FakeAgent(world.broker.home, "k3") as second:
        await second.join("#build", "codex-2")
        await world.review(second, action="raise", title="naming")
        await world.review(second, action="drop", item="F1", reason="nit")
        await world.review(world.reviewer, action="raise", title="off by one")
        await world.review(world.author, action="concede", item="F2")
        await world.review(world.author, action="fix", item="F2", commit=FIX, proof="test_it")
        await world.review(world.reviewer, action="confirm", item="F2")
        await world.ci(world.lead)
        await world.verdict(world.reviewer)
        assert f"codex-2 has no verdict at {SHORT}" in await world.show()
        await second.leave("#build")
    shown = await world.show()
    assert f"Ready at {SHORT} (codex-2 left without a verdict)" in shown

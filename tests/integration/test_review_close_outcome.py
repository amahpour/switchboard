"""A board closes with its pull request's outcome, and a pull request that ends closes its board
(#886, DESIGN.md §37.11, §37.14), worked through the real ``review`` tool by scripted agents."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard import review_stats, reviews
from switchboard.config import Config

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
PR = "https://github.com/example/shop/pull/7"
SHA = "0123abc4567def"
PATH = "/api/rooms/build/review"


@pytest.fixture
def broker(tmp_home: Path) -> Iterator[InProcBroker]:
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    web.close()
    yield b
    b.stop()


def q(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


async def tool(agent: FakeAgent, **args: Any) -> dict[str, Any]:
    return await agent.call("review", room="#build", **args)


def outcome_of(broker: InProcBroker, url: str = PR) -> str:
    web = broker.web_client()
    try:
        rows = web.get(PATH + "/past").json()["boards"]
    finally:
        web.close()
    return next(b["outcome"] for b in rows if b["url"] == url)


async def test_a_close_with_only_a_reason_still_works_reads_unknown_and_asks_for_the_outcome(
    broker: InProcBroker,
) -> None:
    """Plugin 1.x skills call close with a reason alone, and a minor release only adds. Fails if a
    close with no ``state`` is refused, or reads anything but Unknown, or its reply does not ask for
    the outcome in one line; a ``state`` that is given is still checked."""
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        assert (await tool(agent, action="open", url=PR))["ok"]
        refused = await tool(agent, action="close", state="done")
        assert refused["ok"] is False and "merged, closed, unknown" in refused["error"]
        assert q(broker, "SELECT closed_at FROM reviews")[0]["closed_at"] is None
        closed = await tool(agent, action="close", reason=f"merged as {SHA}")
        assert closed["ok"], closed
        note = closed["note"]
        assert "\n" not in note
        assert all(
            word in note for word in ('state="merged"', "commit=", 'state="closed"', 'state="unknown"')
        )
        assert "report(state=...)" in note
        assert q(broker, "SELECT closed_at, pr_state FROM reviews")[0]["pr_state"] == ""
        assert outcome_of(broker) == "unknown"
        assert (await tool(agent, action="report", url=PR, state="closed"))["ok"]
        assert outcome_of(broker) == "closed"


async def test_a_board_closed_without_an_outcome_takes_a_closed_report_and_keeps_its_closer(
    broker: InProcBroker,
) -> None:
    """A person closes a board on the page, which records only the close. An agent's later
    ``report state="closed"`` is accepted the way ``merged`` is, keeping the first closer and time, and
    never overwrites a recorded merge. Fails if the report is refused or moves the closer, or if it
    turns a merged board into closed."""
    other = "https://github.com/example/shop/pull/8"
    web = broker.web_client()
    try:
        async with FakeAgent(broker.home, "k1") as agent:
            await agent.join("#build", "claude-1")
            for url in (PR, other):
                assert (await tool(agent, action="open", url=url))["ok"]
            assert (
                web.post(
                    PATH, json={"action": "close", "url": PR}, headers=broker.write_headers()
                ).status_code
                == 200
            )
            [before] = q(broker, "SELECT closed_at, closed_by FROM reviews WHERE url=?", PR)
            assert before["closed_at"] is not None and before["closed_by"] == "alice"
            assert (await tool(agent, action="report", url=PR, state="closed"))["ok"]
            [after] = q(broker, "SELECT closed_at, closed_by, pr_state FROM reviews WHERE url=?", PR)
            assert (after["closed_at"], after["closed_by"], after["pr_state"]) == (
                before["closed_at"], "alice", "closed",
            )  # fmt: skip
            assert outcome_of(broker, PR) == "closed"
            assert (await tool(agent, action="close", url=other, state="merged", commit=SHA))["ok"]
            clash = await tool(agent, action="report", url=other, state="closed")
            assert clash["ok"] is False and "merged" in clash["error"], clash
            assert outcome_of(broker, other) == "merged"
    finally:
        web.close()


async def test_a_merged_close_needs_the_merge_commit_and_records_what_report_records(
    broker: InProcBroker,
) -> None:
    """``close`` with outcome merged is a merge report: the board's pull request state, who reported
    it and when, and a ``report`` event, which is what Past boards, the Usage page and review stats
    read. Fails if merged is accepted with no commit, if the board still reads unknown, or if the
    stats do not see the merge."""
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        assert (await tool(agent, action="open", url=PR))["ok"]
        bare = await tool(agent, action="close", state="merged")
        assert bare["ok"] is False and "commit" in bare["error"]
        wrong = await tool(agent, action="close", state="merged", commit="not a sha")
        assert wrong["ok"] is False
        closed = await tool(agent, action="close", state="merged", commit=SHA, reason="shipped")
        assert closed["ok"], closed
        [rv] = q(broker, "SELECT * FROM reviews")
        assert (rv["pr_state"], rv["reported_by"], rv["closed_by"]) == ("merged", "claude-1", "claude-1")
        assert rv["reported_at"] is not None and rv["closed_at"] is not None
        assert outcome_of(broker) == "merged"
        events = [
            (r["ts"], json.loads(r["data"]))
            for r in q(broker, "SELECT ts, data FROM events WHERE kind='review' ORDER BY id")
        ]
        reports = [d["report"] for _, d in events if "report" in d]
        assert reports == [{"pr_state": "merged", "commit": SHA}]
        assert review_stats.merged_at(dict(rv), events) is not None
        assert any(SHA in n["text"] for n in q(broker, "SELECT text FROM messages WHERE kind='notice'"))


async def test_a_close_without_merging_and_a_close_said_unknown_on_purpose(broker: InProcBroker) -> None:
    """Closed without merging is recorded as the pull request's state ``closed``; unknown leaves it
    unset. A commit is only the merge commit, so it is refused with any other outcome. Fails if a
    closed pull request reads merged or unknown, or if an unknown close claims a state."""
    other = "https://github.com/example/shop/pull/8"
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        assert (await tool(agent, action="open", url=PR))["ok"]
        assert (await tool(agent, action="open", url=other))["ok"]
        stray = await tool(agent, action="close", url=PR, state="closed", commit=SHA)
        assert stray["ok"] is False and "merged" in stray["error"]
        assert (await tool(agent, action="close", url=PR, state="closed"))["ok"]
        assert (await tool(agent, action="close", url=other, state="unknown"))["ok"]
        states = {r["url"]: r["pr_state"] for r in q(broker, "SELECT url, pr_state FROM reviews")}
        assert states == {PR: "closed", other: ""}
        assert outcome_of(broker, PR) == "closed"
        assert outcome_of(broker, other) == "unknown"
        assert q(broker, "SELECT reported_by FROM reviews WHERE url=?", other)[0]["reported_by"] == ""


async def test_a_report_that_the_pull_request_closed_closes_the_board_and_a_merged_one_too(
    broker: InProcBroker,
) -> None:
    """The close of a pull request or merge request comes from an agent's report, since switchboard
    holds no forge token. Both ends close the board, keep it, and show on Past boards. Fails if a
    report of closed leaves the board open, or reads unknown."""
    other = "https://github.com/example/shop/pull/8"
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        for url in (PR, other):
            assert (await tool(agent, action="open", url=url))["ok"]
            assert (await tool(agent, action="raise", url=url, title="a gap"))["ok"]
        assert (await tool(agent, action="report", url=PR, state="closed"))["closed"]
        assert (await tool(agent, action="report", url=other, state="merged"))["closed"]
        assert outcome_of(broker, PR) == "closed" and outcome_of(broker, other) == "merged"
        assert not q(broker, "SELECT 1 FROM reviews WHERE closed_at IS NULL")


async def test_a_room_full_of_boards_with_work_is_freed_by_reporting_one_ended_and_the_refusal_says_how(
    broker: InProcBroker,
) -> None:
    """The eight-board refusal cannot strand a room: it names the boards that fill a slot and the
    ways to close one, and a report that a pull request merged frees its slot at once. Fails if the
    refusal names no board, or if a reported merge leaves the room refused."""
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        urls = [f"https://github.com/example/shop/pull/{n}" for n in range(1, reviews.MAX_OPEN_BOARDS + 1)]
        for url in urls:
            assert (await tool(agent, action="open", url=url))["ok"]
            assert (await tool(agent, action="raise", url=url, title="a gap"))["ok"]
        extra = "https://github.com/example/shop/pull/90"
        full = await tool(agent, action="open", url=extra)
        assert full["ok"] is False and all(u in full["error"] for u in urls), full
        assert "merged, closed or unknown" in full["error"]
        assert (await tool(agent, action="report", url=urls[3], state="merged"))["ok"]
        assert (await tool(agent, action="open", url=extra))["ok"]


async def test_open_boards_have_a_ceiling_settled_ones_included_and_a_close_never_needs_room(
    broker: InProcBroker,
) -> None:
    """Settled boards take no slot of eight, so the room as a whole is bounded another way: at most
    ``MAX_OPEN_BOARDS_TOTAL`` open boards, each update building only those. Fails if a settled board
    past the ceiling can be opened, reopened or its refusal names no settled board, if a close needs
    room, or if an update carries more boards than the ceiling."""
    total = reviews.MAX_OPEN_BOARDS_TOTAL
    web = broker.web_client()
    try:
        async with FakeAgent(broker.home, "k1") as agent:
            await agent.join("#build", "claude-1")
            urls = [f"https://github.com/example/shop/pull/{n}" for n in range(1, total + 1)]
            for url in urls:
                assert (await tool(agent, action="open", url=url))["ok"]
                assert (await tool(agent, action="raise", url=url, title="gap"))["ok"]
                assert (await tool(agent, action="drop", url=url, item="F1", reason="not a bug"))["ok"]
            extra = "https://github.com/example/shop/pull/99"
            refused = await tool(agent, action="open", url=extra)
            assert refused["ok"] is False and urls[0] in refused["error"], refused
            assert len(web.get(PATH).json()["boards"]) == total
            assert (await tool(agent, action="close", url=urls[0], state="unknown"))["ok"]
            assert (await tool(agent, action="open", url=extra))["ok"]
            back = await tool(agent, action="open", url=urls[0])  # a reopen counts too
            assert back["ok"] is False and urls[1] in back["error"], back
            assert len(web.get(PATH).json()["boards"]) == total
    finally:
        web.close()

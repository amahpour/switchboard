"""Review boards are kept when closed, a room holds several, and a close is guarded (#445, DESIGN.md
§37.14), worked through the real ``review`` tool by scripted agents and a person on the web routes."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker, cookie_of, ws_connect
from fakes.fake_agent import FakeAgent

from switchboard import reviews
from switchboard.config import Config

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
PR_A = "https://github.com/example/shop/pull/7"
PR_B = "https://github.com/example/shop/pull/8"
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


def notices(b: InProcBroker) -> list[str]:
    return [r["text"] for r in q(b, "SELECT text FROM messages WHERE kind='notice' ORDER BY id")]


def boards(web: Any) -> list[dict[str, Any]]:
    return web.get(PATH).json()["boards"]


async def tool(agent: FakeAgent, **args: Any) -> dict[str, Any]:
    return await agent.call("review", room="#build", **args)


async def test_closing_a_board_keeps_it_and_open_brings_back_its_items_history_and_moves_the_head(
    broker: InProcBroker,
) -> None:
    """A closed board is read-only but whole: ``show(url)`` reads every item, ``list`` names it,
    a move on it is refused, and ``open(url)`` brings it back as the same board with its history,
    at the newer head (unsettled findings marked outdated). Fails if closing loses cards, a closed
    board can't be read, or re-opening starts empty (the bug in #445)."""
    web = broker.web_client()
    try:
        async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
            await author.join("#build", "claude-1")
            await reviewer.join("#build", "codex-1")
            await tool(reviewer, action="open", url=PR_A, head="aaa1111")
            await tool(reviewer, action="raise", title="wrong total", where="shop/cart.py:40")
            await tool(reviewer, action="raise", title="rounding", where="shop/cart.py:50")
            await tool(author, action="concede", item="F1")
            await tool(author, action="fix", item="F1", commit="bbb2222", proof="test_total")
            await tool(reviewer, action="confirm", item="F1")
            closed = await tool(reviewer, action="close", reason="moving on")
            assert closed["ok"] and closed["closed"] and "is kept" in closed["note"], closed
            assert boards(web) == []

            shown = await tool(author, action="show", url=PR_A)
            assert shown["ok"], shown
            head_line, *lines = shown["board"].splitlines()
            assert head_line.startswith(f"Review of {PR_A} at aaa1111: open (closed by codex-1 on ")
            assert head_line.endswith("; open(url) brings it back)")
            assert any(x.startswith("F2 [raised] rounding") for x in lines)
            assert any(x.startswith("F1 [fixed] wrong total") for x in lines)
            listed = (await tool(author, action="list"))["boards"]
            assert f"{PR_A} at aaa1111 [closed]: 1 open, 1 settled, opened by codex-1 on " in listed
            assert ", closed by codex-1 on " in listed
            refused = await tool(author, action="raise", url=PR_A, title="late")
            assert refused["ok"] is False and "is closed: open it again" in refused["error"]

            back = await tool(reviewer, action="open", url=PR_A, head="ccc3333")
            assert back["ok"] and "open again" in back["note"], back
            [board] = boards(web)
            assert (
                board["head"] == "ccc3333"
                and board["closed_at"] is None
                and board["reopened_by"] == "codex-1"
            )
            items = {i["label"]: i for i in board["items"]}
            assert (items["F1"]["state"], items["F2"]["state"]) == ("fixed", "raised")
            assert items["F2"]["outdated_head"] == "ccc3333" and items["F1"]["outdated_head"] == ""
            assert [h["did"] for h in items["F1"]["history"]] == [
                "raised it", "conceded it", "fixed it", "confirmed it",
            ]  # fmt: skip
        rows = q(broker, "SELECT id, closed_at, closed_by FROM reviews")
        assert [tuple(r) for r in rows] == [(1, None, "")]  # one board, open again
        assert len(q(broker, "SELECT id FROM review_items")) == 2
        said = notices(broker)
        assert "Review board for #7 closed by codex-1: moving on. Open items: F2." in said
        assert f"codex-1 opened the review board for {PR_A} again: its items and history are back" in said
    finally:
        web.close()


async def test_a_room_holds_several_open_boards_and_a_call_without_url_goes_to_the_one_last_used(
    broker: InProcBroker,
) -> None:
    """Opening a second pull request's board no longer needs the first closed (the refusal in #445).
    Each board numbers its own items, a call without ``url`` is about the board the agent last acted
    on, and an agent that has acted on none is asked which, with the open boards listed, rather than
    given a guess. Whoever is told of a move on a board has that board as theirs: the wake line names
    it, so answering it can't land on another. Fails if a board is refused, labels collide, or a
    call lands on a board the agent never meant."""
    web = broker.web_client()
    try:
        async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
            await author.join("#build", "claude-1")
            await reviewer.join("#build", "codex-1")
            assert (await tool(reviewer, action="open", url=PR_A))["ok"]
            assert (await tool(reviewer, action="open", url=PR_B))["ok"]  # A stays open
            assert [b["url"] for b in boards(web)] == [PR_A, PR_B]

            await tool(reviewer, action="raise", title="on B, the last opened")  # no url: B
            await tool(reviewer, action="raise", url=PR_A, title="on A")
            by_url = {b["url"]: [i["title"] for i in b["items"]] for b in boards(web)}
            assert by_url == {PR_A: ["on A"], PR_B: ["on B, the last opened"]}  # each has its own F1
            shown = await tool(reviewer, action="show")  # the board last acted on is A now
            assert shown["board"].startswith(f"Review of {PR_A}")

            # devin-1 joins after the moves: it has acted on neither board and was told of neither
            async with FakeAgent(broker.home, "k3") as late:
                await late.join("#build", "devin-1")
                lost = await tool(late, action="concede", item="F1")
                assert lost["ok"] is False and "has 2 open boards: say which with url=" in lost["error"]
                assert PR_A in lost["error"] and PR_B in lost["error"]
            # the wake line for the finding on A names A and every call it suggests carries the url
            told = q(
                broker, "SELECT text FROM messages WHERE kind='chat' AND sender_name='codex-1' ORDER BY id"
            )
            assert told[-1]["text"].startswith(f"Review board {PR_A}: F1 raised: on A. @claude-1:")
            assert f'review(action="show", url="{PR_A}")' in told[-1]["text"]
            # ... and it made A the board claude-1's next call is about
            conceded = await tool(author, action="concede", item="F1")
            assert conceded["ok"] and conceded["board"].startswith(f"Review of {PR_A}"), conceded
            b_item = await tool(author, action="concede", item="F1", url=PR_B)
            assert b_item["ok"] and b_item["board"].startswith(f"Review of {PR_B}")
            history = {b["url"]: b["items"][0]["history"] for b in boards(web)}
            assert [h["did"] for h in history[PR_A]] == ["raised it", "conceded it"]
            assert [h["did"] for h in history[PR_B]] == ["raised it", "conceded it"]  # not each other's
    finally:
        web.close()


async def test_a_room_holds_at_most_a_handful_of_open_boards(broker: InProcBroker) -> None:
    """Open boards are capped so a member can't fill the page with tabs; closing one makes room, and
    a board brought back counts. Fails if the cap is missing or ignores a re-opened board."""
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        urls = [f"https://github.com/example/shop/pull/{n}" for n in range(1, reviews.MAX_OPEN_BOARDS + 2)]
        for u in urls[:-1]:
            assert (await tool(agent, action="open", url=u))["ok"]
        refused = await tool(agent, action="open", url=urls[-1])
        assert refused["ok"] is False and "the most it holds" in refused["error"], refused
        assert (await tool(agent, action="close", url=urls[0]))["ok"]
        assert (await tool(agent, action="open", url=urls[-1]))["ok"]
        again = await tool(agent, action="open", url=urls[0])  # the room is full again
        assert again["ok"] is False and "the most it holds" in again["error"]


async def test_only_the_holder_or_a_person_closes_a_board_and_anyone_elses_close_asks_in_the_room(
    broker: InProcBroker,
) -> None:
    """The board's holder (who opened it, or opened it again last) closes it; another agent's close
    changes nothing, needs a reason, and says once in the room that it asks a person; a person closes
    any board from the page. Every close names who. Fails if a bystander can close a board silently
    (the third problem in #445), asks twice, or the person's close isn't recorded."""
    web = broker.web_client()
    try:
        async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
            await author.join("#build", "claude-1")
            await reviewer.join("#build", "codex-1")
            await tool(author, action="open", url=PR_A)
            await tool(author, action="raise", title="tax order")

            bare = await tool(reviewer, action="close", url=PR_A)
            assert bare["ok"] is False and "reason is required" in bare["error"]
            for _ in range(2):
                refused = await tool(reviewer, action="close", url=PR_A, reason="stale")
                assert refused["ok"] is False
                assert "#7 was opened by claude-1: only claude-1 or a person can close it" in refused["error"]
            assert len(boards(web)) == 1  # nothing changed
            asks = [n for n in notices(broker) if "asks a person to close" in n]
            assert asks == [
                "codex-1 asks a person to close the review board for #7 (opened by claude-1): stale"
            ]
            assert not [n for n in notices(broker) if "closed by" in n]

            gone = web.post(PATH, json={"action": "close", "url": PR_A}, headers=broker.write_headers())
            assert gone.status_code == 200 and gone.json()["boards"] == []
            assert "alice closed the review board for " + PR_A in notices(broker)
            row = q(broker, "SELECT closed_by FROM reviews")[0]
            assert row["closed_by"] == "alice"

            # opened again by codex-1, it is codex-1's to close, and no longer claude-1's
            assert (await tool(reviewer, action="open", url=PR_A))["ok"]
            [board] = boards(web)
            assert (board["opened_by"], board["reopened_by"]) == ("claude-1", "codex-1")
            no = await tool(author, action="close", reason="mine")
            assert no["ok"] is False and "only codex-1 or a person" in no["error"]
            yes = await tool(reviewer, action="close", reason="done")
            assert yes["ok"] and yes["closed"]
            assert q(broker, "SELECT closed_by FROM reviews")[0]["closed_by"] == "codex-1"
            assert any(n.startswith("Review board for #7 closed by codex-1: done") for n in notices(broker))
    finally:
        web.close()


async def test_a_person_names_the_board_they_move_and_the_room_hears_which(broker: InProcBroker) -> None:
    """The page sends the board's ``url`` with every move; without one, a room with several open
    boards refuses rather than guess. A person's decision message names the board by its number
    (never its URL, #177) and sends agents to ``list`` instead of to a board that may not be theirs;
    a handoff names its board too, and the agent chosen has that board as its own. Fails if a move
    can land on the wrong board, a URL goes into the person's words, or the handoff is ambiguous."""
    web = broker.web_client()
    try:
        async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
            await author.join("#build", "claude-1")
            await reviewer.join("#build", "codex-1")
            await tool(reviewer, action="open", url=PR_A)
            await tool(reviewer, action="open", url=PR_B)
            await tool(reviewer, action="ask", url=PR_A, title="Before or after tax?",
                       options=["before", "after"], recommend=0)  # fmt: skip
            await tool(reviewer, action="raise", url=PR_B, title="stale cache", where="shop/cache.py:9")

            def chat() -> list[str]:
                return [r["text"] for r in q(broker, "SELECT text FROM messages WHERE sender_kind='human'")]

            vague = web.post(
                PATH, json={"action": "answer", "item": "Q1", "option": 0}, headers=broker.write_headers()
            )
            assert vague.status_code == 400 and "2 open boards: say which with url" in vague.json()["message"]
            assert chat() == []
            answered = web.post(
                PATH, json={"action": "answer", "item": "Q1", "option": 1, "url": PR_A},
                headers=broker.write_headers(),
            )  # fmt: skip
            assert answered.status_code == 200
            a, b = answered.json()["boards"]
            assert a["items"][0]["answer"] == "after" and b["items"][0]["state"] == "raised"
            assert chat() == [
                'Review board #7: Q1 answered with option 1. review(action="list") shows the boards.'
            ]
            assert all("http" not in text for text in chat())

            dropped = web.post(
                PATH, json={"action": "drop", "item": "F1", "reason": "not a bug", "url": PR_B},
                headers=broker.write_headers(),
            )  # fmt: skip
            assert dropped.status_code == 200
            sent = web.post(
                PATH,
                json={"action": "handoff", "agent": "claude-1", "url": PR_B},
                headers=broker.write_headers(),
            )
            assert sent.status_code == 200, sent.text
            hand_off = chat()[-1]
            assert hand_off.startswith(
                "Hand off: @claude-1, post one review to this board's PR or MR (#8) with"
            )
            assert "http" not in hand_off
            # the chosen agent's `show` is the board handed off, though it last acted on nothing
            mine = await tool(author, action="show")
            assert mine["board"].startswith(f"Review of {PR_B}") and "handed off to claude-1" in mine["board"]
            # the other board is untouched, and the person can close either by url
            assert [x["url"] for x in boards(web)] == [PR_A, PR_B]
            closed = web.post(PATH, json={"action": "close", "url": PR_A}, headers=broker.write_headers())
            assert [x["url"] for x in closed.json()["boards"]] == [PR_B]
            unknown = web.post(PATH, json={"action": "close", "url": PR_A}, headers=broker.write_headers())
            assert unknown.status_code == 400 and "no open board for" in unknown.json()["message"]
    finally:
        web.close()


async def test_a_boards_history_and_handoff_decisions_are_its_own_though_labels_repeat(
    broker: InProcBroker,
) -> None:
    """F1 is on every board, so a person's ruling on B's F1 must not turn up in A's handoff plan or on
    A's F1 card. Every review event names its board. Fails if events are read by label alone (a line
    comment on the wrong pull request)."""
    web = broker.web_client()
    try:
        async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
            await author.join("#build", "claude-1")
            await reviewer.join("#build", "codex-1")
            for url, who in ((PR_A, "agents"), (PR_B, "a person")):
                await tool(reviewer, action="open", url=url)
                await tool(
                    reviewer, action="raise", url=url, title=f"settled by {who}", where="shop/cart.py:3-4"
                )
            await tool(reviewer, action="drop", url=PR_A, item="F1", reason="agents agree")
            ruled = web.post(
                PATH, json={"action": "drop", "item": "F1", "reason": "not worth it", "url": PR_B},
                headers=broker.write_headers(),
            )  # fmt: skip
            assert ruled.status_code == 200
            plans = {b["url"]: b["plan"] for b in boards(web)}
            assert plans[PR_A]["comments"] == []  # its drop was an agent's, not a person's
            [comment] = plans[PR_B]["comments"]
            assert comment["item"] == "F1" and "alice dropped this finding" in comment["body"]
            hist = {b["url"]: [(h["by"], h["did"]) for h in b["items"][0]["history"]] for b in boards(web)}
            assert hist[PR_A] == [("codex-1", "raised it"), ("codex-1", "dropped it")]
            assert hist[PR_B] == [("codex-1", "raised it"), ("alice", "dropped it")]
    finally:
        web.close()


async def test_reset_starts_a_fresh_board_and_keeps_the_old_one_in_the_database(broker: InProcBroker) -> None:
    """``reset`` is on purpose: the url, a reason, and the board's holder. The fresh board is empty and
    its history starts over, while the earlier board's items and events stay stored and nothing is
    deleted. Fails if a bystander can reset, a reset needs no reason, the old rows are dropped, or the
    fresh board's F1 reads the old F1's moves."""
    web = broker.web_client()
    try:
        async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
            await author.join("#build", "claude-1")
            await reviewer.join("#build", "codex-1")
            await tool(author, action="open", url=PR_A)
            await tool(author, action="raise", title="old finding")
            await tool(reviewer, action="concede", item="F1")

            other = await tool(reviewer, action="reset", url=PR_A, reason="start over")
            assert other["ok"] is False and "only claude-1 can reset it" in other["error"]
            for missing in ({"reason": "r"}, {"url": PR_A}):
                no = await tool(author, action="reset", **missing)
                assert no["ok"] is False and (
                    "url is required" in no["error"] or "reason is required" in no["error"]
                )
            unknown = await tool(author, action="reset", url=PR_B, reason="r")
            assert unknown["ok"] is False and f"no board for {PR_B}" in unknown["error"]
            assert [i["title"] for i in boards(web)[0]["items"]] == ["old finding"]  # nothing happened

            fresh = await tool(author, action="reset", url=PR_A, reason="the PR was rebuilt")
            assert fresh["ok"] and fresh["board"].splitlines() == [f"Review of {PR_A}: settled"], fresh
            [board] = boards(web)
            assert board["items"] == [] and board["id"] == 2
            await tool(author, action="raise", title="new finding")
            [item] = boards(web)[0]["items"]
            assert [h["did"] for h in item["history"]] == ["raised it"]  # not the old F1's concession
        rows = q(broker, "SELECT id, url, closed_by, closed_at IS NULL AS open FROM reviews ORDER BY id")
        assert [(r["id"], r["url"], r["closed_by"], r["open"]) for r in rows] == [
            (1, PR_A, "claude-1", 0), (2, PR_A, "", 1),
        ]  # fmt: skip
        items = q(broker, "SELECT review_id, title FROM review_items")
        assert [(r["review_id"], r["title"]) for r in items] == [(1, "old finding"), (2, "new finding")]
        on_old = q(broker, "SELECT id FROM events WHERE kind='review' AND data LIKE '%\"board\": 1}'")
        assert len(on_old) >= 3  # its open, its concession and the reset itself are still on it
        assert any(
            n.startswith("claude-1 reset the review board for #7: the PR was rebuilt")
            for n in notices(broker)
        )
    finally:
        web.close()


async def test_list_names_each_board_once_even_after_a_reset(broker: InProcBroker) -> None:
    """A URL's board is its newest row, so ``list`` shows a reset board once, and open before closed.
    Fails if the older row of a reset shows up as a second board."""
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await tool(agent, action="open", url=PR_A)
        await tool(agent, action="open", url=PR_B)
        await tool(agent, action="close", url=PR_B)
        await tool(agent, action="reset", url=PR_A, reason="again")
        text = (await tool(agent, action="list"))["boards"]
        lines = [x for x in text.splitlines() if x.startswith("https://")]
        assert [x.split(" ")[0] for x in lines] == [PR_A, PR_B]
        assert (
            "[open]" in lines[0] and "[closed]" in lines[1] and "your calls without url go here" in lines[0]
        )
        assert text.splitlines()[0] == "2 review boards in this room, open ones first:"


async def test_a_merge_reported_on_one_board_closes_that_board_and_says_who(broker: InProcBroker) -> None:
    """A reported merge ends the one board it is about, with the reporter as the closer, and leaves the
    other open and the page given only it. Fails if a merge closes the room's other board, or the
    closer isn't recorded."""
    web = broker.web_client()
    try:
        async with FakeAgent(broker.home, "k1") as agent:
            await agent.join("#build", "claude-1")
            await tool(agent, action="open", url=PR_A)
            await tool(agent, action="open", url=PR_B)
            done = await tool(agent, action="report", url=PR_A, state="merged")
            assert done["ok"] and done["closed"] is True
            assert [b["url"] for b in boards(web)] == [PR_B]
            row = q(broker, "SELECT closed_by FROM reviews WHERE url=?", PR_A)[0]
            assert row["closed_by"] == "claude-1"
    finally:
        web.close()


async def test_every_move_gives_the_page_all_the_open_boards_and_the_last_close_gives_it_none(
    broker: InProcBroker,
) -> None:
    """The page replaces what it has with each ``review`` frame, so a frame holds every open board,
    and closing the last sends an empty list so its board button goes. Fails if a frame carries only
    the board that moved (the other tab would vanish) or the last close sends nothing."""
    web = broker.web_client()
    ws = ws_connect(broker, cookie_of(web))

    def frame(until: Any, timeout: float = 10.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            assert left > 0, "no review frame with the awaited boards"
            f = json.loads(ws.recv(timeout=left))
            if f.get("t") == "review" and until(f["boards"]):
                return f["boards"]

    try:
        ws.send(json.dumps({"t": "hello", "rooms": ["#build"], "after": {}}))
        async with FakeAgent(broker.home, "k1") as agent:
            await agent.join("#build", "claude-1")
            await tool(agent, action="open", url=PR_A)
            await tool(agent, action="open", url=PR_B)
            await tool(agent, action="raise", url=PR_A, title="only A moved")
            both = frame(lambda bs: len(bs) == 2 and bs[0]["items"])
            assert [b["url"] for b in both] == [PR_A, PR_B]
            await tool(agent, action="close", url=PR_A, reason="r")
            assert [b["url"] for b in frame(lambda bs: len(bs) == 1)] == [PR_B]
            await tool(agent, action="close", url=PR_B)
            assert frame(lambda bs: bs == []) == []
    finally:
        ws.close()
        web.close()

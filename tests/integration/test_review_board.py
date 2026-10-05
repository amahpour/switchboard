"""A review board worked through the real ``review`` tool by two scripted agents against an
in-process broker (#80, DESIGN.md §37): moves are notices that wake no one, the board is the
record, and every refusal says what to fix."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard.config import Config

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
PR = "https://example.com/shop/pull/7"


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    yield b
    web.close()
    b.stop()


def q(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


async def test_a_board_from_open_to_settled(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
        await author.join("#build", "claude-1")
        await reviewer.join("#build", "codex-1")

        r = await reviewer.call("review", room="#build", action="open", url=PR, head="abc1234")
        assert r["ok"] and r["board"].startswith(f"Review of {PR} at abc1234: settled")
        r = await reviewer.call(
            "review", room="#build", action="raise", title="discount applied after tax",
            file="shop/cart.py", lines="40-58", detail="total() taxes first",
        )  # fmt: skip
        assert (
            r["item"] == "F1" and "F1 [raised] discount applied after tax (shop/cart.py:40-58)" in r["board"]
        )
        r = await author.call(
            "review", room="#build", action="ask", title="Discount before or after tax?",
            options=["before tax", "after tax"], recommend=0,
        )  # fmt: skip
        assert r["item"] == "Q1"

        # the raiser can't concede its own finding; the author concedes, naming itself owner
        r = await reviewer.call("review", room="#build", action="concede", item="F1")
        assert r["ok"] is False and "is yours" in r["error"]
        r = await author.call("review", room="#build", action="concede", item="F1")
        assert "F1 [conceded]" in r["board"] and "owner claude-1" in r["board"]
        r = await reviewer.call("review", room="#build", action="fix", item="F1", commit="def5678")
        assert r["ok"] is False and "claude-1's to fix" in r["error"]
        r = await author.call("review", room="#build", action="fix", item="F1", commit="def5678")
        assert "F1 [fixed]" in r["board"] and r["settled"] is False  # Q1 is still a person's

        r = await author.call("review", room="#build", action="show")
        assert r["board"].splitlines()[1].startswith("Q1 [open]")

    notices = [m["text"] for m in q(broker, "SELECT text FROM messages WHERE kind='notice' ORDER BY id")]
    assert [n for n in notices if "F1" in n or "Q1" in n or "board" in n] == [
        f"codex-1 opened a review board for {PR} at abc1234",
        "codex-1 raised F1: discount applied after tax",
        "claude-1 asked Q1: Discount before or after tax?",
        "claude-1 conceded F1, owner claude-1",
        "claude-1 fixed F1 in def5678",
    ]
    # no move is a chat message, so none was ever delivered to anyone
    assert q(broker, "SELECT COUNT(*) FROM messages WHERE kind='chat'")[0][0] == 0
    rows = q(broker, "SELECT kind, n, state, owner, commit_sha FROM review_items ORDER BY kind, n")
    assert [tuple(r) for r in rows] == [
        ("finding", 1, "fixed", "claude-1", "def5678"),
        ("question", 1, "open", "", ""),
    ]


async def test_refusals_say_what_to_fix(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "claude-1")
        r = await a.call("review", room="#build", action="raise", title="x")
        assert r["ok"] is False and 'open one with action="open"' in r["error"]
        r = await a.call("review", room="#build", action="open", url="file:///etc/passwd")
        assert r["ok"] is False and "http(s) link" in r["error"]
        await a.call("review", room="#build", action="open", url=PR)
        r = await a.call("review", room="#build", action="open", url=PR + "8")
        assert r["ok"] is False and "a person closes it" in r["error"]
        r = await a.call("review", room="#build", action="concede", item="F9")
        assert r["ok"] is False and "has no F9" in r["error"]
        r = await a.call("review", room="#build", action="tidy")
        assert r["ok"] is False and "action must be one of" in r["error"]
        r = await a.call("review", room="#build", action="raise", title="t" * 201)
        assert r["ok"] is False and "too long" in r["error"]
        await a.call("review", room="#build", action="raise", title="mine")
        r = await a.call("review", room="#build", action="drop", item="F1")
        assert r["ok"] is False and "reason is required" in r["error"]


async def test_a_conceded_finding_needs_an_owner_in_the_room(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
        await author.join("#build", "claude-1")
        await reviewer.join("#build", "codex-1")
        await reviewer.call("review", room="#build", action="open", url=PR)
        await reviewer.call("review", room="#build", action="raise", title="x")
        r = await author.call("review", room="#build", action="concede", item="F1", owner="devin-9")
        assert r["ok"] is False and "owner must be an agent in #build" in r["error"]
        r = await author.call("review", room="#build", action="concede", item="F1", owner="codex-1")
        assert "owner codex-1" in r["board"]


async def test_a_person_answers_rules_and_closes_from_the_web(broker: InProcBroker) -> None:
    """The person's 10% (§37.5): answering a question and ruling on a contested finding are their
    own messages in the room, so they reach the agents; closing the board is a notice."""
    web = broker.web_client()
    try:
        async with FakeAgent(broker.home, "k1") as author, FakeAgent(broker.home, "k2") as reviewer:
            await author.join("#build", "claude-1")
            await reviewer.join("#build", "codex-1")
            await reviewer.call("review", room="#build", action="open", url=PR)
            await reviewer.call("review", room="#build", action="raise", title="rounding")
            await author.call(
                "review", room="#build", action="contest", item="F1", reason="banker's rounding"
            )
            await author.call(
                "review",
                room="#build",
                action="ask",
                title="Before or after tax?",
                options=["before", "after"],
            )

            got = web.get("/api/rooms/build/review").json()["board"]
            assert [(i["label"], i["state"], i["needs_person"]) for i in got["items"]] == [
                ("F1", "contested", True),
                ("Q1", "open", True),
            ]
            assert got["counts"]["needs_person"] == 2 and got["settled"] is False

            def move(**body: Any) -> Any:
                return web.post("/api/rooms/build/review", json=body, headers=broker.write_headers())

            r = move(action="answer", item="Q1", option=5)
            assert r.status_code == 400 and "0 to 1" in r.json()["message"]
            r = move(action="answer", item="F1", option=0)
            assert r.status_code == 400 and "can't be answered" in r.json()["message"]
            r = move(action="answer", item="Q1", option=1)
            assert r.status_code == 200 and r.json()["board"]["items"][1]["answer"] == "after"
            r = move(action="concede", item="F1", owner="codex-1")
            assert r.status_code == 200 and r.json()["board"]["items"][0]["owner"] == "codex-1"

            # the agents hear both decisions as alice's own messages
            text = (await author.read("#build"))["text"]
            assert "Review board: Q1 (Before or after tax?) answered: after" in text
            assert "Review board: F1 (rounding) conceded, owner codex-1" in text

            r = move(action="close")
            assert r.status_code == 200 and r.json()["board"] is None
            assert web.get("/api/rooms/build/review").json()["board"] is None
            # closed: the room can open a board for another pull request now
            r = await reviewer.call("review", room="#build", action="open", url=PR + "8")
            assert r["ok"] and r["board"].startswith(f"Review of {PR}8")
    finally:
        web.close()

    chat = q(broker, "SELECT sender_kind, text FROM messages WHERE kind='chat' ORDER BY id")
    assert [(r["sender_kind"], r["text"][:21]) for r in chat] == [
        ("human", "Review board: Q1 (Bef"),
        ("human", "Review board: F1 (rou"),
    ]
    assert any(
        "alice closed the review board" in r["text"]
        for r in q(broker, "SELECT text FROM messages WHERE kind='notice'")
    )


def test_the_board_routes_need_a_signed_in_person_and_the_write_header(broker: InProcBroker) -> None:
    import httpx

    anon = httpx.Client(base_url=broker.base, timeout=10.0)
    web = broker.web_client()
    try:
        assert anon.get("/api/rooms/build/review").status_code == 401
        r = anon.post("/api/rooms/build/review", json={"action": "close"}, headers=broker.write_headers())
        assert r.status_code == 401
        r = web.post("/api/rooms/build/review", json={"action": "close"}, headers={"Origin": broker.origin})
        assert r.status_code == 403  # no X-Switchboard: not from the page
        r = web.post(
            "/api/rooms/build/review", json={"action": "close", "x": 1}, headers=broker.write_headers()
        )
        assert r.status_code == 400
        r = web.post("/api/rooms/build/review", json={"action": "close"}, headers=broker.write_headers())
        assert r.status_code == 400 and "no review board" in r.json()["message"]
    finally:
        web.close()
        anon.close()

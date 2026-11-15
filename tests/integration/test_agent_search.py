"""``read(query=...)``: an agent searches its own room's history (#738, DESIGN.md §58.8).

A scripted FakeAgent (a real MCP client of ``/mcp``) against an in-process broker. What these catch: an agent
reading another room's lines through a search; a search that moves the agent's read state (it must stay a
plain view, like ``before=``); a limiter shared with people, or one that never bites; a query that reaches
the index as syntax, or an unfiltered dump for a query with no words; a room the agent never joined."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FakeClock, InProcBroker
from fakes.fake_agent import FakeAgent, ids_in
from test_mcp_agent import FAST, q, states

from switchboard import search
from switchboard.broker.search import MAX_PER_WINDOW


@pytest.fixture
def broker(tmp_home: Path) -> Iterator[InProcBroker]:
    clock = FakeClock()
    b = InProcBroker(tmp_home, FAST, clock=clock).start()
    web = b.web_client()
    for name in ("#build", "#side"):
        assert web.post("/api/rooms", json={"name": name}, headers=b.write_headers()).status_code == 200
    b.web = web
    b.fake_clock = clock  # type: ignore[attr-defined]
    yield b
    web.close()
    b.stop()


def post(b: InProcBroker, room: str, text: str) -> int:
    r = b.web.post(f"/api/rooms/{room}/say", json={"text": text}, headers=b.write_headers())
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_an_agent_finds_an_old_message_by_a_word_in_it(broker: InProcBroker) -> None:
    """The point of the feature: a decision far back in the room, past what one read() can page to, is found
    by a word in it, newest first, with its id and its full text; "quoted phrase" matches as a phrase."""
    old = post(broker, "build", "we chose the sqlite backend for the ledger")
    for i in range(60):
        post(broker, "build", f"filler {i}")
    newer = post(broker, "build", "the ledger needs a backup job")
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        r = await a.read("#build", query="ledger")
        assert r["ok"] and r["batch_id"] is None and r["count"] == 2 and not r["more"]
        assert ids_in(r["text"]) == [newer, old]  # newest first
        assert "we chose the sqlite backend for the ledger" in r["text"]
        assert r["text"].startswith("[switchboard] #build: 2 matches, newest first.")
        assert (await a.read("#build", query='"sqlite backend"'))["count"] == 1
        assert (await a.read("#build", query="ledger zzzz"))["count"] == 0  # every word must match
        assert (await a.read("#build", query="ledger", limit=1))["more"] is True


async def test_a_search_never_returns_another_rooms_lines(broker: InProcBroker) -> None:
    """The same word said in another room is not returned, even though the agent is a member of both: a
    search is of the one room the call names."""
    mine = post(broker, "build", "rotate the zephyr key")
    other = post(broker, "side", "zephyr is secret in side")
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        await a.join("#side", "tester")
        r = await a.read("#build", query="zephyr")
        assert ids_in(r["text"]) == [mine]
        assert "secret in side" not in r["text"] and f"id={other}" not in r["text"]


async def test_a_search_leaves_unread_messages_unread(broker: InProcBroker) -> None:
    """A search is a view, as before= is: a message it returns stays pending, the membership row is
    untouched, and the real read() still delivers it. Were it a delivery, the agent would lose the message."""
    async with FakeAgent(broker.home, "ka") as a, FakeAgent(broker.home, "kb") as b:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        mid = (await b.say("#build", "@alpha the parser drops port 0"))["posted_id"]
        assert states(broker, "alpha")[mid] == "pending"
        before = q(broker, "SELECT * FROM memberships WHERE screen_name='alpha'")
        r = await a.read("#build", query="parser")
        assert r["ok"] and ids_in(r["text"]) == [mid] and r["batch_id"] is None
        assert states(broker, "alpha")[mid] == "pending"
        assert q(broker, "SELECT * FROM memberships WHERE screen_name='alpha'") == before  # no cursor moved
        rd = await a.read("#build")  # the real read still delivers it: the search did not use it up
        assert ids_in(rd["text"]) == [mid] and rd["batch_id"] is not None


async def test_call_lines_are_searched_too(broker: InProcBroker) -> None:
    """The matching is the web UI's: a line from a call (sender kind meeting) is found like any other."""

    def insert() -> int:
        store = broker.state.store
        room = store.get_room("#build")
        assert room is not None
        return store.insert_message(
            room.id, sender_name="bob", sender_kind="meeting", via="meeting", text="the quorum vote passed"
        ).id

    mid = int(broker.on_loop(insert))
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        r = await a.read("#build", query="quorum")
        assert r["count"] == 1 and ids_in(r["text"]) == [mid] and "quorum vote passed" in r["text"]


async def test_agent_searches_are_rate_limited_per_agent(broker: InProcBroker) -> None:
    """The people's limit, counted per agent: the call after MAX_PER_WINDOW in a window is refused with a
    wait, one agent's use does not spend another's, and the window passes."""
    post(broker, "build", "alpha")
    clock = broker.fake_clock  # type: ignore[attr-defined]
    async with FakeAgent(broker.home, "k1") as a, FakeAgent(broker.home, "k2") as b:
        await a.join("#build", "one")
        await b.join("#build", "two")
        for _ in range(MAX_PER_WINDOW):
            assert (await a.read("#build", query="alpha"))["count"] == 1
        slow = await a.read("#build", query="alpha")
        assert slow["ok"] and slow["reason"] == "rate_limited" and slow["count"] == 0
        assert slow["retry_after_s"] >= 1 and "too many searches" in slow["text"]
        assert (await b.read("#build", query="alpha"))["count"] == 1  # its own bucket
        assert (await a.read("#build"))["ok"]  # an ordinary read is not a search
        clock.advance(11)
        assert (await a.read("#build", query="alpha"))["count"] == 1


@pytest.mark.parametrize("query", ["!!! ??? ...", '""', "   ", "AND OR NEAR", "-", "*"])
async def test_a_query_with_no_words_returns_nothing_not_everything(broker: InProcBroker, query: str) -> None:
    """Punctuation, a bare operator or blanks make no words: a short empty answer, never an error and never
    the room's whole history (an unfiltered dump)."""
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        post(broker, "build", "something said here")  # unread: a read that ignored the query would show it
        r = await a.read("#build", query=query)
        assert r["ok"] and r["count"] == 0, r
        assert "something said here" not in r["text"]


async def test_query_syntax_is_words_not_index_syntax(broker: InProcBroker) -> None:
    """FTS5 operators and column filters in a query are only words (the web UI's rule): none of these raises
    and a column filter does not reach another column."""
    post(broker, "build", "plain ok line")
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        for q_ in ["text:plain", "plain NEAR ok", "plain OR zzz", '"unbalanced', "plain*", "(plain"]:
            r = await a.read("#build", query=q_)
            assert r["ok"], (q_, r)


async def test_a_long_query_is_cut_like_the_web_routes(broker: InProcBroker) -> None:
    """A query past the cap is cut (to MAX_QUERY_CHARS) rather than refused or trusted: words after the cut
    are not searched, so a long run of padding then a real word finds nothing, and the call still answers."""
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        post(broker, "build", "needle")
        long_q = ("x" * 10 + " ") * 40 + "needle"
        assert len(long_q) > search.MAX_QUERY_CHARS
        r = await a.read("#build", query=long_q)
        assert r["ok"] and r["count"] == 0
        huge = await a.read("#build", query="needle " + "y" * 1_000_000)
        assert huge["ok"]


async def test_query_with_before_is_refused(broker: InProcBroker) -> None:
    """The two choose different views; combining them has no clear meaning, so it is refused, not guessed."""
    i = post(broker, "build", "needle")
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        r = await a.read("#build", query="needle", before=i + 1)
        assert r["ok"] is False and r["code"] == "bad_request" and "cannot be used together" in r["error"]


async def test_a_room_the_agent_has_not_joined_is_refused(broker: InProcBroker) -> None:
    """A search needs the membership read() needs: no join, no search, and no hint of what the room holds."""
    post(broker, "side", "hidden zephyr")
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        r = await a.read("#side", query="zephyr")
        assert r["ok"] is False and r["code"] == "not_member"
        assert "hidden zephyr" not in str(r)

"""A board written before secrets were cut (#890) stays usable, and nothing new copies its secret out.

The rows here are written straight to the store, as an older release left them: a title, a proof and a
board's URL that hold a secret. What these catch: a new move (a contest, a reopen, a fix over an earlier fix)
that copies a stored title or proof into a line posted in the room or into an event; a board listed by its
URL that no call can then open, show, move or reset; and a board's own URL echoed, in a list, a page or an
error, as stored. The secrets are built from pieces, so no scanner reads this file as a leak."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from test_review_board import FAST, PR

SECRET = "ghp" + "_" + "aB3" * 12
PASSWORD = "hunter2" * 2
OLD_PR = f"{PR}?access_token={PASSWORD}"
WORD = "https://example.com/shop/pull/9"


def plant(b: InProcBroker, url: str, *, items: list[dict]) -> int:
    """A board of an older release: a row and its cards, written as they were."""
    st = b.state.store

    def write() -> int:
        rv = st.open_review(st.get_room("#build").id, url, "", "codex-1")
        for spec in items:
            it = st.add_review_item(rv["id"], "finding", "raised", title=spec["title"], raised_by="codex-1")
            if spec.get("proof"):
                st.move_review_item(
                    it.id, "fixed", owner="claude-1", commit_sha="abc1234", proof=spec["proof"]
                )
            else:
                st.move_review_item(it.id, "conceded" if spec.get("owner") else "raised", owner="claude-1")
        return rv["id"]

    return b.on_loop(write)


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


async def test_a_move_on_an_older_board_copies_no_secret_into_the_room_or_an_event(
    broker: InProcBroker,
) -> None:
    """A contest builds its line from the stored title, a reopen and a second fix copy the stored proof into
    an event: all new writes, from an older card. Fails if the secret is in any message of the room or in
    any event of the board afterwards."""
    bid = plant(
        broker,
        PR,
        items=[
            {"title": f"leaks {SECRET}"},
            {"title": "second", "proof": f"test_total {SECRET}"},
            {"title": "third", "proof": f"test_total {SECRET}"},
        ],
    )
    async with FakeAgent(broker.home, "la") as author, FakeAgent(broker.home, "lr") as reviewer:
        await author.join("#build", "claude-1")
        await reviewer.join("#build", "codex-1")
        for agent, kw in (
            (author, dict(action="contest", item="F1", reason="not a bug")),
            (reviewer, dict(action="reopen", item="F2", reason="still wrong")),
            (author, dict(action="fix", item="F3", commit="abc1234", proof="test_new")),
        ):
            r = await agent.call("review", room="#build", url=PR, **kw)
            assert r.get("ok") is not False, r
    messages = broker.web.get("/api/rooms/build/messages").json()["messages"]
    events = broker.on_loop(lambda: broker.state.store.review_events(1, bid, 0))
    assert events, "the moves were not recorded"
    assert SECRET not in json.dumps(messages), "the stored secret was posted in the room"
    assert SECRET not in json.dumps([e.data for e in events]), "the stored secret was copied into an event"
    assert "[redacted: github token]" in json.dumps(messages)  # the contest's line, cut and still sent


async def test_a_board_with_an_older_url_is_still_found_shown_moved_and_reset(broker: InProcBroker) -> None:
    """The URL a board was stored under would be refused now, so a lookup can't use the check a new board's
    URL gets: ``list`` and the page show it, and every call that names it must still find it, by the URL as
    shown (the stored password cut out) or as stored. Fails if one is refused, and if the stored password
    is in anything read, listed, posted or in an error."""
    plant(broker, OLD_PR, items=[{"title": "wrong total"}])
    plant(broker, WORD, items=[])
    async with FakeAgent(broker.home, "la") as author, FakeAgent(broker.home, "lr") as reviewer:
        await author.join("#build", "claude-1")
        await reviewer.join("#build", "codex-1")
        listed = (await reviewer.call("review", room="#build", action="list"))["boards"]
        assert PASSWORD not in listed and WORD in listed
        shown_as = next(line.rsplit(" [", 2)[0] for line in listed.splitlines() if "/pull/7" in line)
        reads = []
        for url in (shown_as, OLD_PR):
            r = await reviewer.call("review", room="#build", action="show", url=url)
            assert r.get("ok") is not False and "wrong total" in r["board"], r
            reads.append(json.dumps(r))
            r = await author.call("review", room="#build", action="report", url=url, ci="passing")
            assert r.get("ok") is not False, r
            reads.append(json.dumps(r))
        r = await author.call("review", room="#build", action="concede", item="F1", url=shown_as)
        assert r.get("ok") is not False, r
        reads.append(json.dumps(r))
        gone = await reviewer.call(
            "review", room="#build", action="show", url=OLD_PR.replace("/pull/7", "/pull/8")
        )
        assert gone.get("ok") is False and PASSWORD not in json.dumps(gone), gone
        page = broker.web.get("/api/rooms/build/review").json()["boards"]
        old = next(b for b in page if "/pull/7" in b["url"])
        assert old["url"] == shown_as and PASSWORD not in json.dumps(page)
        r = broker.web.post(
            "/api/rooms/build/review",
            json={"url": old["url"], "action": "drop", "item": "F1", "reason": "fine"},
            headers=broker.write_headers(),
        )
        assert r.status_code == 200, r.text
        assert PASSWORD not in r.text
        r = await reviewer.call("review", room="#build", action="reset", url=shown_as, reason="start over")
        assert r.get("ok") is not False and PASSWORD not in json.dumps(r), r
    messages = broker.web.get("/api/rooms/build/messages").json()["messages"]
    assert PASSWORD not in json.dumps(messages) + "".join(reads)
    # the open board is still a board of the same address: a reset writes no URL it was given
    stored = broker.on_loop(lambda: [b["url"] for b in broker.state.store.room_reviews(1)])
    assert OLD_PR in stored and not any(PASSWORD in u and u != OLD_PR for u in stored)

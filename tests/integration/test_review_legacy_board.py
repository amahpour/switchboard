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
        assert r.get("ok") is False and "can't be reset" in r["error"] and PASSWORD not in json.dumps(r), r
    messages = broker.web.get("/api/rooms/build/messages").json()["messages"]
    assert PASSWORD not in json.dumps(messages) + "".join(reads)
    assert len(stored_urls(broker)) == 2, "a reset copied the board's row"


def stored_urls(b: InProcBroker) -> list[str]:
    """The raw URL of every row of the reviews table: what is on disk, not what is shown."""
    return b.on_loop(lambda: [r[0] for r in b.state.store.con.execute("SELECT url FROM reviews").fetchall()])


async def test_a_reset_of_an_older_board_writes_no_second_copy_of_its_secret(broker: InProcBroker) -> None:
    """A reset opens a fresh row under the URL, so a board whose stored URL holds a secret can't be reset:
    the refusal names no value and nothing is written. Fails if the fresh row is inserted (two rows hold the
    password) or the old board is closed first."""
    plant(broker, OLD_PR, items=[{"title": "wrong total"}])
    async with FakeAgent(broker.home, "lr") as reviewer:
        await reviewer.join("#build", "codex-1")
        for url in (OLD_PR, "https://example.com/shop/pull/7?access_token=[redacted: password or token]"):
            r = await reviewer.call("review", room="#build", action="reset", url=url, reason="again")
            assert (
                r.get("ok") is False and "can't be reset" in r["error"] and PASSWORD not in json.dumps(r)
            ), r
    assert stored_urls(broker) == [OLD_PR]
    closed = broker.on_loop(lambda: broker.state.store.room_reviews(1)[0]["closed_at"])
    assert closed is None


async def test_a_url_that_only_reads_alike_finds_no_board(broker: InProcBroker) -> None:
    """A board is found by its stored URL, or by the identifier it is shown under, and by nothing that
    merely reads like it once cut: a full link that was never registered selects no board. Fails if a
    different secret in the same place finds the older board."""
    plant(broker, OLD_PR, items=[{"title": "wrong total"}])
    other = OLD_PR.replace(PASSWORD, "another" * 2)
    async with FakeAgent(broker.home, "lr") as reviewer:
        await reviewer.join("#build", "codex-1")
        r = await reviewer.call("review", room="#build", action="show", url=other)
        assert r.get("ok") is False and "no board" in r["error"] and "another" not in json.dumps(r), r
        r = await reviewer.call("review", room="#build", action="show", url=OLD_PR)
        assert r.get("ok") is not False, r


async def test_two_boards_shown_alike_are_ambiguous_by_that_name_and_open_by_their_own(
    broker: InProcBroker,
) -> None:
    """Two older boards whose URLs differ only inside the secret read the same: the shown name is refused
    as ambiguous, each stored URL still opens its own. Fails if the shown name picks one."""
    plant(broker, OLD_PR, items=[])
    plant(broker, OLD_PR.replace(PASSWORD, "another" * 2), items=[])
    async with FakeAgent(broker.home, "lr") as reviewer:
        await reviewer.join("#build", "codex-1")
        shown = reviewer_shown = reviews_scrub(OLD_PR)
        r = await reviewer.call("review", room="#build", action="show", url=shown)
        assert r.get("ok") is False and "more than one" in r["error"], r
        assert reviewer_shown
        r = await reviewer.call("review", room="#build", action="show", url=OLD_PR)
        assert r.get("ok") is not False, r


def reviews_scrub(u: str) -> str:
    from switchboard import reviews

    return reviews.scrub(u)


async def test_a_closed_older_board_opens_again_by_its_stored_or_shown_url(broker: InProcBroker) -> None:
    """``open`` and a person's ``/review`` resolve an existing board before the check a new board's URL
    gets, and a shown URL may be longer than the 500 it was stored in. Fails if either URL is refused, if
    the board is not the same row, or if the secret is in the reply."""
    long_url = PR + "?" + "&".join(["token=" + "a1b2c3d4e5"] * 20)
    assert len(long_url) <= 500 and len(reviews_scrub(long_url)) > 500
    bid = plant(broker, long_url, items=[{"title": "wrong total"}])
    broker.on_loop(lambda: broker.state.store.set_review(bid, closed_at=1.0, closed_by="codex-1"))
    async with FakeAgent(broker.home, "lr") as reviewer:
        await reviewer.join("#build", "codex-1")
        for url in (long_url, reviews_scrub(long_url)):
            r = await reviewer.call("review", room="#build", action="open", url=url)
            assert r.get("ok") is not False and "a1b2c3d4e5" not in json.dumps(r), r
            broker.on_loop(lambda: broker.state.store.set_review(bid, closed_at=1.0, closed_by="codex-1"))
        for url in (long_url,):  # a shown URL has spaces, which end a command's word
            r = broker.web.post(
                "/api/rooms/build/command",
                json={"text": f"/review {url}"},
                headers=broker.write_headers(),
            )
            assert r.status_code == 200 and "a1b2c3d4e5" not in r.text, r.text
            broker.on_loop(lambda: broker.state.store.set_review(bid, closed_at=1.0, closed_by="codex-1"))
    assert stored_urls(broker) == [long_url]


async def test_nothing_that_shows_an_older_card_shows_its_secret(broker: InProcBroker) -> None:
    """A card written before the check holds its words as written; the page's data, the tool's reply, the
    handoff plan and a card's history are all read from it. Fails if the title, the proof or the reason is
    in the page, in ``show`` of the board or of one card, or in the reply to a move."""
    plant(
        broker,
        PR,
        items=[{"title": f"leaks {SECRET}", "proof": f"test_total {SECRET}"}, {"title": f"two {SECRET}"}],
    )
    async with FakeAgent(broker.home, "la") as author, FakeAgent(broker.home, "lr") as reviewer:
        await author.join("#build", "claude-1")
        await reviewer.join("#build", "codex-1")
        reads = [
            await reviewer.call("review", room="#build", action="show"),
            await reviewer.call("review", room="#build", action="show", item="F1"),
            await author.call("review", room="#build", action="contest", item="F2", reason="no"),
            await reviewer.call("review", room="#build", action="reopen", item="F1", reason="still"),
        ]
    page = broker.web.get("/api/rooms/build/review").json()
    seen = json.dumps(reads) + json.dumps(page) + broker.web.get("/api/home").text
    assert SECRET not in seen and "[redacted: github token]" in seen

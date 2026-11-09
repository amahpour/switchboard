"""The home page's data over the web API (#626, DESIGN.md §55): ``GET /api/home`` and ``POST /api/home/seen``.

Rows are planted straight into the store, as the other web-route tests do, and every answer is given through
the route the room already has (``say``, the board's person moves), so what these check is that a card
points at the same thing the room shows, goes away when it is answered, and is only ever one person's.
The hosted broker and its people are ``test_people``'s.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from conftest import InProcBroker
from test_passkeys import hosted_broker, stop
from test_people import Browser, join_as_bob, set_up

from switchboard.models import session_key

PR = "https://example.com/shop/pull/7"


@pytest.fixture
def team() -> Iterator[tuple[InProcBroker, Browser, Browser]]:
    """A hosted broker: alice (the owner) and bob, with #build and #uploads, and claude-1 in #build."""
    b = hosted_broker()
    admin = set_up(b)
    bob = join_as_bob(b, admin)
    try:
        for name in ("#build", "#uploads"):
            assert admin.post("/api/rooms", {"name": name}).status_code == 200

        def seed() -> None:
            store = b.state.store
            p = store.upsert_participant("test", session_key("test", "", "claude-1"), host="")
            for name in ("#build", "#uploads"):
                room = store.get_room(name)
                assert room is not None
                store.create_membership(room.id, p.id, "claude-1")

        b.on_loop(seed)
        yield b, admin, bob
    finally:
        bob.close()
        admin.close()
        stop(b)


def agent_says(b: InProcBroker, room: str, text: str, mentions: list[str] | None = None, **kw: Any) -> int:
    def run() -> int:
        store = b.state.store
        r = store.get_room(room)
        assert r is not None
        m = store.find_member(r.id, "claude-1")
        assert m is not None
        return store.insert_message(
            r.id,
            sender_name="claude-1",
            sender_kind="agent",
            via="mcp",
            text=text,
            sender_membership_id=m.membership_id,
            sender_harness="claude",
            mentions=mentions or [],
            **kw,
        ).id

    return int(b.on_loop(run))


def home(who: Browser) -> dict[str, Any]:
    r = who.get("/api/home")
    assert r.status_code == 200, r.text
    return r.json()


def test_a_question_to_you_waits_until_you_say_anything_in_that_room(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """An agent's @mention of alice is a card for alice and not for bob; a reply through the room's own
    ``say`` takes it away, and what alice posted is in the room. Fails if the card is shown to someone it
    was not addressed to, or is still there after the answer, or an answer from home is not the room's."""
    b, alice, bob = team
    qid = agent_says(b, "#build", "@alice keep the old upload endpoint, or remove it?", ["alice"])
    got = home(alice)
    assert [(c["kind"], c["room"], c["id"], c["from"]) for c in got["waiting"]] == [
        ("question", "#build", qid, "claude-1")
    ]
    assert got["waiting"][0]["text"].startswith("@alice keep the old upload endpoint")
    assert home(bob)["waiting"] == []
    sent = alice.post("/api/rooms/build/say", {"text": "Remove it.", "reply_to": qid})
    assert sent.status_code == 200
    assert home(alice)["waiting"] == []
    texts = [m["text"] for m in alice.get("/api/rooms/build/messages").json()["messages"]]
    assert texts[-1] == "Remove it."


def test_a_reply_to_you_and_at_humans_wait_but_chatter_between_agents_does_not(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """The page's own test for what is addressed to a person: an @mention, @humans, or a reply to what they
    said. Plain agent chatter is not. Fails if any of those is dropped or the chatter is counted."""
    b, alice, _ = team
    mine = alice.post("/api/rooms/uploads/say", {"text": "what is the limit?"}).json()["id"]
    agent_says(b, "#uploads", "just working", [])
    assert home(alice)["waiting"] == []
    agent_says(b, "#uploads", "ten megabytes", [], reply_to=mine)
    assert [c["kind"] for c in home(alice)["waiting"]] == ["question"]
    agent_says(b, "#uploads", "@humans ship it?", ["humans"])
    only = home(alice)["waiting"]
    assert len(only) == 1 and only[0]["more"] == 1  # one card a room, with the count under it


def test_a_board_call_is_a_card_and_answering_it_is_the_rooms_own_move(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """A question on the review board that needs a person is a card with its options; answering it through
    the board's route clears the card and posts the same line in the room. Fails if a settled board shows a
    card, if the card hides the options, or the answer is not the board's."""
    b, alice, _ = team

    def seed() -> None:
        store = b.state.store
        room = store.get_room("#build")
        assert room is not None
        rv = store.open_review(room.id, PR, "", "claude-1")
        store.add_review_item(
            rv["id"], "question", "open", title="Keep v1?", raised_by="claude-1", options=["Keep", "Remove"]
        )
        store.add_review_item(rv["id"], "finding", "dropped", title="old", raised_by="claude-1")

    b.on_loop(seed)
    cards = [c for c in home(alice)["waiting"] if c["kind"] == "board"]
    assert len(cards) == 1 and cards[0]["need"] == 1
    item = cards[0]["items"][0]
    assert (item["label"], item["moves"], item["options"]) == ("Q1", ["answer"], ["Keep", "Remove"])
    r = alice.post("/api/rooms/build/review", {"action": "answer", "item": "Q1", "option": 1, "url": PR})
    assert r.status_code == 200, r.text
    assert [c for c in home(alice)["waiting"] if c["kind"] == "board"] == []
    texts = [m["text"] for m in alice.get("/api/rooms/build/messages").json()["messages"]]
    assert any("Q1 answered with option 1" in t for t in texts)


def test_done_lists_what_agents_said_since_you_looked_and_seen_moves_only_forward(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """An agent's message not addressed to alice is a done line, the latest one per agent per room;
    ``seen`` empties the list, a later ``seen`` with an older time changes nothing, and a bad body is
    refused. Fails if a message that waits is also done, a line repeats, or the time can go back."""
    b, alice, _ = team
    agent_says(b, "#build", "first thing", [])
    agent_says(b, "#build", "## raised the `upload` limit\nsecond line", [])
    agent_says(b, "#build", "@alice a question", ["alice"])
    got = home(alice)
    assert [(d["kind"], d["who"], d["text"]) for d in got["done"]] == [
        ("turn", "claude-1", "raised the upload limit")
    ]
    assert alice.post("/api/home/seen", {"at": "soon"}).status_code == 400
    assert alice.post("/api/home/seen", {"nope": 1}).status_code == 400
    now = got["now"]
    assert alice.post("/api/home/seen", {"at": now + 5}).status_code == 200
    assert home(alice)["done"] == []
    after = alice.post("/api/home/seen", {"at": now - 1000}).json()["seen"]
    assert after >= now
    assert home(alice)["done"] == []


def test_a_board_move_line_is_not_done_work_and_a_pass_leaves_no_line(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """The lines the review board posts for an agent belong to the board, so they are not "done"; and an agent
    that only passed said nothing, so it has no line at all (the maintainer's answer to #626's third
    question).
    Fails if a board line is listed, or a member that never spoke has a line."""
    b, alice, _ = team
    agent_says(b, "#build", 'Review board: F1 raised: Old path. review(action="show") shows the board.', [])
    assert home(alice)["done"] == []


def test_live_shows_a_call_and_an_agent_at_work_and_a_private_room_only_to_its_people(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """A call in the room and a busy agent with its status line are live; a private room alice made is not
    in bob's page at all (cards, done or live). Fails if a live item is missing or a private room leaks."""
    b, alice, bob = team
    assert alice.post("/api/rooms", {"name": "#secret", "private": True}).status_code == 200

    def seed() -> None:
        store = b.state.store
        for name in ("#build", "#secret"):
            room = store.get_room(name)
            assert room is not None
            if name == "#build":
                m = store.find_member(room.id, "claude-1")
                assert m is not None
                store.set_status(m.participant_id, "busy", "test")
                store.update_participant(m.participant_id, status_line="Reviewing the upload fix")
            else:
                store.insert_meeting(room.id, started_by="alice", platform="zoom")

    b.on_loop(seed)
    live = home(alice)["live"]
    assert [(x["kind"], x["room"]) for x in live] == [
        ("call", "#secret"),
        ("agent", "#build"),
        ("agent", "#uploads"),  # the same session is in both rooms: busy in each
    ]
    assert live[1]["note"] == "Reviewing the upload fix" and live[1]["busy"] is True
    assert [x["room"] for x in home(bob)["live"]] == ["#build", "#uploads"]

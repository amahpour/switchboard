"""The store's part of a review board's lines (#884, DESIGN.md §37.17): a line reaches only the
members it addresses, the lines a board's members still have open are found from what was recorded
when each was posted, and answering one sticks, whatever state its delivery was in."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.models import session_key
from switchboard.store import Store

PR = "https://github.com/example/shop/pull/7"


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "lines.db"), clock)


def members(store: Store, room_id: int, *names: str) -> dict[str, int]:
    out = {}
    for name in names:
        p = store.upsert_participant("test", session_key("test", "", name))
        out[name] = store.create_membership(room_id, p.id, name).id
    return out


def line(store: Store, room_id: int, sender: int, to: list[str], board: int, cards: dict[str, str]) -> int:
    msg = store.insert_message(
        room_id,
        sender_name="codex-1",
        sender_kind="agent",
        via="mcp",
        text="Review board: F1 raised: off by one. @claude-1: concede or contest it.",
        sender_membership_id=sender,
        mentions=to,
        addressed_only=True,
    )
    store.add_event(
        "review_line",
        room_id=room_id,
        data={"board": board, "msg": msg.id, "items": list(cards), "states": list(cards.values()), "to": to},
    )
    return msg.id


def states(store: Store, membership_id: int) -> dict[int, str]:
    return {d["message_id"]: d["state"] for d in store.deliveries(membership_id)}


def test_a_line_reaches_only_the_members_it_addresses(store: Store) -> None:
    """A board's line for whoever acts next has a delivery row for them alone. Fails if a bystander gets
    it as chatter, which wakes an idle agent once the unaddressed hold ends (#884)."""
    room = store.create_room("#build", "alice", 60, 0)
    m = members(store, room.id, "codex-1", "claude-1", "grok-1")
    board = store.open_review(room.id, PR, "", "claude-1")["id"]
    msg = line(store, room.id, m["codex-1"], ["claude-1"], board, {"F1": "raised"})
    assert states(store, m["claude-1"]) == {msg: "pending"}
    assert states(store, m["grok-1"]) == {}
    plain = store.insert_message(room.id, sender_name="codex-1", sender_kind="agent", via="mcp", text="hi")
    assert states(store, m["grok-1"]) == {plain.id: "pending"}  # an ordinary say still reaches everyone


def test_a_live_claim_the_line_names_still_reaches_its_holder(store: Store, clock: FakeClock) -> None:
    """A claim is a watch term (§8.1): its holder is addressed by a message that names it, a board's line
    too. Fails if addressed-only meant the @mentions alone."""
    room = store.create_room("#build", "alice", 60, 0)
    m = members(store, room.id, "codex-1", "claude-1", "grok-1")
    store.move_claim(room.id, m["grok-1"], PR, 600)
    msg = store.insert_message(
        room.id,
        sender_name="codex-1",
        sender_kind="agent",
        via="mcp",
        text=f"Review board {PR}: F1 raised: off by one. @claude-1: concede or contest it.",
        sender_membership_id=m["codex-1"],
        mentions=["claude-1"],
        addressed_only=True,
    )
    [d] = store.deliveries(m["grok-1"])
    assert d["message_id"] == msg.id and d["mentioned"] == 1


def test_open_lines_are_this_boards_with_a_current_member_still_holding_them(store: Store) -> None:
    room = store.create_room("#build", "alice", 60, 0)
    m = members(store, room.id, "codex-1", "claude-1", "grok-1")
    board = store.open_review(room.id, PR, "", "claude-1")["id"]
    other = store.open_review(room.id, PR.replace("/7", "/8"), "", "claude-1")["id"]
    first = line(
        store, room.id, m["codex-1"], ["claude-1", "grok-1"], board, {"F1": "raised", "F2": "raised"}
    )
    line(store, room.id, m["codex-1"], ["claude-1"], other, {"F1": "raised"})
    assert store.review_lines_open(room.id, board, 0.0) == [
        (
            first,
            [("F1", "raised"), ("F2", "raised")],
            [(m["claude-1"], "claude-1"), (m["grok-1"], "grok-1")],
            ["claude-1", "grok-1"],
        )
    ]
    store.answer_deliveries([(m["grok-1"], first)])
    store.end_membership(m["claude-1"], "leave")
    assert store.review_lines_open(room.id, board, 0.0) == []  # one answered it, the other left


def test_an_answered_line_stays_answered_through_its_batch_confirming_or_expiring(store: Store) -> None:
    """A line can be answered while its batch is still offered (the move came before the hook that
    confirms it). Fails if the confirmation, or an expiry, put it back in context or pending, where the
    turn's end would bring it back as again=yes."""
    room = store.create_room("#build", "alice", 60, 0)
    m = members(store, room.id, "codex-1", "claude-1")
    board = store.open_review(room.id, PR, "", "claude-1")["id"]
    for end in ("confirm", "expire"):
        msg = line(store, room.id, m["codex-1"], ["claude-1"], board, {"F1": "raised"})
        b = store.create_batch(m["claude-1"], path="wait", kind="wake", items=[(msg, True)])
        assert states(store, m["claude-1"])[msg] == "offered"
        assert store.answer_deliveries([(m["claude-1"], msg)]) == 1
        if end == "confirm":
            store.confirm_batch(b.id, "test")
        else:
            store.expire_batch(b.id, "test")
        assert states(store, m["claude-1"])[msg] == "handled", end
    assert store.answer_deliveries([(m["claude-1"], msg)]) == 0  # handled already: nothing to do

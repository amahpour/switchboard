"""store: participants, memberships, batches, two-phase confirmation, cursor_id (DESIGN.md §4, §8.7)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.store import Store, StoreError


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "y.db"), clock)


def setup(store: Store):
    room = store.create_room("#build", "alice", 60, 6)
    p = store.upsert_participant(
        "test", "test:a", status="busy", agent_pid=10, agent_start=1.0, mcp_pid=11, mcp_start=2.0
    )
    m = store.create_membership(room.id, p.id, "alpha", "hash-a")
    return room, p, m


def test_participant_upsert_and_reactivate(store: Store) -> None:
    room, p, m = setup(store)
    assert store.find_participant("test", "test:a").id == p.id
    assert store.membership_by_cred("hash-a").id == m.id
    ended = store.end_participant(p.id)
    assert [x.id for x in ended] == [m.id]
    assert store.get_participant(p.id).ended_at is not None
    assert store.membership_by_cred("hash-a") is None
    p2 = store.upsert_participant("test", "test:a", status="busy")
    assert p2.id == p.id and p2.active
    with pytest.raises(ValueError):
        store.upsert_participant("test", "test:b", bogus=1)


def test_mcp_lookup_uses_start_time(store: Store) -> None:
    _room, p, _m = setup(store)
    assert [x.id for x in store.participants_by_mcp("", 11, 2.0)] == [p.id]
    assert store.participants_by_mcp("", 11, 9.0) == []


def test_name_reuse_and_kick_memory(store: Store, clock: FakeClock) -> None:
    room, p, m = setup(store)
    q = store.upsert_participant("test", "test:b", status="busy")
    assert store.name_used_by_other(room.id, "ALPHA", q.id, clock.now() - 86400)
    store.end_membership(m.id, "kick", kicked=True)
    assert store.was_kicked(room.id, p.id)
    clock.advance(86401)
    assert not store.name_used_by_other(room.id, "alpha", q.id, clock.now() - 86400)


def test_kick_memory_follows_a_cursor_conversation(store: Store) -> None:
    """A kick of any earlier participant of the conversation (its key, or an ended one
    renamed ``<key>#ended-<n>``) counts; the match is exact on the prefix (``_`` is not
    a wildcard), and other conversations don't count."""
    room = store.create_room("#build", "alice", 60, 6)
    key = "cursor:conv_1"
    old = store.upsert_participant("cursor", key, status="busy")
    m = store.create_membership(room.id, old.id, "cursor-1", "h1")
    store.end_membership(m.id, "kick", kicked=True)
    store.update_participant(old.id, session_key=f"{key}#ended-{old.id}")
    new = store.upsert_participant("cursor", key, status="busy")
    assert store.was_kicked_session(room.id, "cursor", key, exclude=new.id)
    assert not store.was_kicked_session(room.id, "cursor", "cursor:convX1", exclude=new.id)  # '_' literal
    assert not store.was_kicked_session(room.id, "cursor", "cursor:conv", exclude=new.id)
    assert not store.was_kicked_session(room.id, "devin", key, exclude=new.id)
    other = store.create_room("#other", "alice", 60, 6)
    assert not store.was_kicked_session(other.id, "cursor", key, exclude=new.id)


def test_batch_confirm_and_cursor(store: Store) -> None:
    room, p, m = setup(store)
    ids = [
        store.insert_message(room.id, sender_name="alice", sender_kind="human", via="web", text=f"t{i}").id
        for i in range(3)
    ]
    b = store.create_batch(m.id, path="read", kind="pull", items=[(ids[1], True), (ids[2], True)])
    assert store.inflight_offer(m.id)
    with pytest.raises(StoreError):
        store.create_batch(m.id, path="read", kind="pull", items=[(ids[1], True)])  # not pending
    store.confirm_batch(b.id, "test")
    # message 0 is still pending, so the cursor can't pass it
    assert store.get_membership(m.id).cursor_id == 0
    b0 = store.create_batch(m.id, path="read", kind="pull", items=[(ids[0], True)])
    store.confirm_batch(b0.id, "test")
    assert store.get_membership(m.id).cursor_id == ids[2]
    assert store.confirm_batch(b0.id, "again") is None  # only offered batches confirm


def test_confirm_chatter_is_handled_and_stubs_are_notified(store: Store) -> None:
    room, p, m = setup(store)
    q = store.upsert_participant("test", "test:b", status="busy")
    mq = store.create_membership(room.id, q.id, "beta", "hash-b")
    chat = store.insert_message(
        room.id, sender_name="beta", sender_kind="agent", via="mcp", text="c", sender_membership_id=mq.id
    )
    ment = store.insert_message(
        room.id,
        sender_name="beta",
        sender_kind="agent",
        via="mcp",
        text="@alpha",
        sender_membership_id=mq.id,
        mentions=["alpha"],
    )
    b = store.create_batch(m.id, path="hook_ctx", kind="priority", items=[(chat.id, True), (ment.id, False)])
    store.confirm_batch(b.id, "ack")
    ds = {d["message_id"]: d for d in store.deliveries(m.id)}
    assert ds[chat.id]["state"] == "handled"
    assert ds[ment.id]["state"] == "pending" and ds[ment.id]["notified_at"] is not None


def test_expire_and_cancel(store: Store) -> None:
    room, p, m = setup(store)
    mid = store.insert_message(room.id, sender_name="alice", sender_kind="human", via="web", text="x").id
    b = store.create_batch(m.id, path="inbox", kind="wake", items=[(mid, True)], counted=True)
    assert store.room_by_id(room.id).budget_remaining == 59
    store.expire_batch(b.id, "no_ack", push=True)
    d = store.deliveries(m.id)[0]
    assert d["state"] == "pending" and d["attempts"] == 1 and d["batch_id"] is None
    assert store.get_participant(p.id).push_expiries == 1
    b2 = store.create_batch(m.id, path="inbox", kind="wake", items=[(mid, True)])
    store.expire_batch(b2.id, "pause", state="cancelled")
    assert store.deliveries(m.id)[0]["attempts"] == 1
    assert store.get_batch(b2.id).state == "cancelled"


def test_end_membership_cancels_open_offers(store: Store) -> None:
    room, p, m = setup(store)
    mid = store.insert_message(room.id, sender_name="alice", sender_kind="human", via="web", text="x").id
    b = store.create_batch(m.id, path="read", kind="pull", items=[(mid, True)])
    store.end_membership(m.id, "leave")
    assert store.get_batch(b.id).state == "cancelled"
    assert store.deliveries(m.id)[0]["state"] == "revoked"


def test_mark_handled_and_unhandled_priority(store: Store) -> None:
    room, p, m = setup(store)
    mid = store.insert_message(room.id, sender_name="alice", sender_kind="human", via="web", text="x").id
    b = store.create_batch(m.id, path="read", kind="pull", items=[(mid, True)])
    store.confirm_batch(b.id, "t")
    assert store.unhandled_priority(m.id) == 1
    assert store.mark_handled(m.id) == 1
    assert store.unhandled_priority(m.id) == 0


def test_budget_notice_once_per_window(store: Store) -> None:
    room, _p, _m = setup(store)
    assert store.mark_budget_notice(room.id, room.budget_window_start)
    assert not store.mark_budget_notice(room.id, room.budget_window_start)
    assert store.mark_budget_notice(room.id, room.budget_window_start + 3600)

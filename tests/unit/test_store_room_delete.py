"""store.delete_room (DESIGN.md §28.6): one transaction, every row that names the room and
nothing else, refused while anyone is in it or when the database moved since its backup."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock
from test_store import add_agent
from switchboard import db
from switchboard.models import Room, closed_room_name
from switchboard.store import ROOM_DELETE_TABLES, Conflict, Store, StoreError


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "y.db"), clock)


def snapshot(con: sqlite3.Connection) -> dict[str, set[tuple]]:
    return {t: {tuple(r) for r in con.execute(f"SELECT * FROM {t}")} for t in db.TABLES}


def rows_of(con: sqlite3.Connection, room_id: int) -> dict[str, set[tuple]]:
    """Every row that names one room: its row, messages, memberships, their deliveries,
    batches and events."""
    mids = "(SELECT id FROM memberships WHERE room_id=:r)"
    q = {
        "rooms": "SELECT * FROM rooms WHERE id=:r",
        "messages": "SELECT * FROM messages WHERE room_id=:r",
        "memberships": "SELECT * FROM memberships WHERE room_id=:r",
        "deliveries": f"SELECT * FROM deliveries WHERE membership_id IN {mids}"
                      " OR message_id IN (SELECT id FROM messages WHERE room_id=:r)",
        "batches": f"SELECT * FROM batches WHERE membership_id IN {mids}",
        "events": f"SELECT * FROM events WHERE room_id=:r OR membership_id IN {mids}",
    }
    return {t: {tuple(r) for r in con.execute(sql, {"r": room_id})} for t, sql in q.items()}


def seed_room(store: Store, name: str, tag: str) -> tuple[Room, list[int], list[int]]:
    """A room with three members (one leaves, one is kicked, one stays), messages with
    deliveries, an offered and a confirmed batch, room events and a membership-only event.
    Returns (room, membership ids, message ids)."""
    r = store.create_room(name, "alice", 60, 6)
    p1, m1 = add_agent(store, r.id, f"{tag}-one")
    p2, m2 = add_agent(store, r.id, f"{tag}-two")
    _, m3 = add_agent(store, r.id, f"{tag}-three")
    h = store.insert_message(r.id, sender_name="alice", sender_kind="human", via="web", text=f"{tag} hi")
    a = store.insert_message(r.id, sender_name=f"{tag}-one", sender_kind="agent", via="mcp", text="ok",
                             sender_membership_id=m1)
    n = store.insert_message(r.id, sender_name="switchboard", sender_kind="system", via="system",
                             kind="notice", text="n")
    confirmed = store.create_batch(m1, path="wait", kind="wake", items=[(h.id, 1)])
    store.confirm_batch(confirmed.id, "wait")
    store.create_batch(m2, path="wait", kind="wake", items=[(h.id, 1)])  # left offered
    store.add_event("pause", room_id=r.id, data={"via": "web"})
    store.add_event("kick", room_id=r.id, membership_id=m2)
    store.add_event("note", membership_id=m1)  # no room_id: found through the membership
    store.add_event("bind", participant_id=p1)  # a participant's own: never the room's
    store.end_membership(m1, "leave")
    store.end_membership(m2, "kick", kicked=True)
    return r, [m1, m2, m3], [h.id, a.id, n.id]


@pytest.fixture
def two(store: Store) -> tuple[Room, Room, list[int], list[int]]:
    """Room B first (lower ids), then room A, which ends empty: A holds the highest room and
    membership ids, so a new room and membership reuse them after the delete."""
    b, _, _ = seed_room(store, "#bravo", "b")
    a, a_mids, a_msgs = seed_room(store, "#alpha", "a")
    store.end_membership(a_mids[2], "closed", keep_cred=True)
    return a, b, a_mids, a_msgs


def delete(store: Store, r: Room, **kw: Any) -> dict[str, int]:
    args: dict[str, Any] = {"name": r.name, "created_at": r.created_at,
                            "expect_counts": db.row_counts(store.con, db.TABLES),
                            "event": {"room_id": r.id, "name": r.name, "backup": "y.db.delete-x.bak", "chain": "cli"}}
    args.update(kw)
    return store.delete_room(r.id, **args)


def test_delete_removes_the_room_and_nothing_else(store: Store, two) -> None:
    a, b, a_mids, a_msgs = two
    con = store.con
    closed = store.rename_room(a.id, closed_room_name(a.name, a.id), expect=a.name)
    parts_before = snapshot(con)["participants"]
    b_before = rows_of(con, b.id)
    counts = store.room_delete_counts(a.id)
    assert counts == {"rooms": 1, "messages": 3, "memberships": 3, "deliveries": 5, "batches": 2, "events": 3}
    assert list(counts) == list(ROOM_DELETE_TABLES)
    total = db.row_counts(con, db.TABLES)
    removed = delete(store, closed)
    assert removed == counts and list(removed) == list(ROOM_DELETE_TABLES)
    after = db.row_counts(con, db.TABLES)
    assert after == {t: total[t] - removed.get(t, 0) + (t == "events") for t in db.TABLES}
    assert rows_of(con, b.id) == b_before  # the other room: every row as it was
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.room_delete_counts(a.id) == dict.fromkeys(ROOM_DELETE_TABLES, 0)
    left = [("deliveries", "membership_id", a_mids), ("deliveries", "message_id", a_msgs),
            ("batches", "membership_id", a_mids), ("events", "membership_id", a_mids),
            ("messages", "id", a_msgs), ("memberships", "id", a_mids)]
    for table, col, ids in left:
        n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} IN ({','.join('?' * len(ids))})", ids)
        assert n.fetchone()[0] == 0, (table, col)
    assert snapshot(con)["participants"] == parts_before  # never deleted
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='bind'").fetchone()[0] == 2  # nor their events
    # the record of it: no room_id (ids are reused), what went, and the caller's fields
    [ev] = store.recent_events(kinds=["room_delete"])
    assert ev.room_id is None and ev.membership_id is None
    assert ev.data == {"room_id": a.id, "name": closed.name, "backup": "y.db.delete-x.bak", "chain": "cli",
                       "removed": removed}
    # a new room takes the freed id, a new membership the freed membership id: nothing old follows
    c = store.create_room("#charlie", "alice", 60, 6)
    assert c.id == a.id
    _, mid = add_agent(store, c.id, "c-one")
    assert mid == a_mids[0]
    assert store.history(c.id) == [] and store.recent_events(room_id=c.id) == []
    assert store.deliveries(mid) == [] and store.offered_batches(mid) == []
    assert con.execute("SELECT COUNT(*) FROM events WHERE membership_id=?", (mid,)).fetchone()[0] == 0
    assert store.latest_close_event(c.id) is None


def test_an_open_room_without_members_can_go(store: Store) -> None:
    r = store.create_room("#scratch", "alice", 60, 6)
    assert delete(store, r) == {"rooms": 1, "messages": 0, "memberships": 0, "deliveries": 0, "batches": 0,
                                "events": 0}
    assert store.get_room("#scratch") is None


@pytest.mark.parametrize("who", ["local", "offline", "remote"])
def test_refused_while_anyone_is_in_the_room(store: Store, two, who: str) -> None:
    a, _, _, _ = two
    pid, _ = add_agent(store, a.id, "late")
    with db.tx(store.con):
        if who == "offline":
            store.con.execute("UPDATE participants SET status='offline' WHERE id=?", (pid,))
        elif who == "remote":
            store.con.execute("UPDATE participants SET host='fpga-pi', session_key='test@fpga-pi:late'"
                              " WHERE id=?", (pid,))
    assert store.members(a.id)[0].host == ("fpga-pi" if who == "remote" else "")
    before = snapshot(store.con)
    with pytest.raises(Conflict, match="#alpha has 1 agent"):
        delete(store, a)
    assert snapshot(store.con) == before


def test_refused_for_another_room_under_the_same_id_and_name(store: Store) -> None:
    """ids are reused after a delete, so the id and name alone could name a re-created room."""
    r = store.create_room("#scratch", "alice", 60, 6)
    before = snapshot(store.con)
    with pytest.raises(Conflict, match="#scratch changed since the plan"):
        delete(store, r, created_at=r.created_at + 1)
    assert snapshot(store.con) == before


def test_refused_when_the_counts_moved_since_the_backup(store: Store, two) -> None:
    a, _, _, _ = two
    counts = db.row_counts(store.con, db.TABLES)
    store.add_event("late", room_id=None)  # a write after the backup
    before = snapshot(store.con)
    with pytest.raises(Conflict, match="the database changed while its backup was made"):
        delete(store, a, expect_counts=counts)
    assert snapshot(store.con) == before


def test_refused_when_the_room_changed_since_the_plan(store: Store, two) -> None:
    a, _, _, _ = two
    before = snapshot(store.con)
    with pytest.raises(Conflict, match="changed since the plan"):
        delete(store, a, name=closed_room_name(a.name, a.id))  # planned as closed, still open
    with pytest.raises(Conflict, match="changed since the plan"):
        store.delete_room(9999, name="#gone", created_at=0.0, expect_counts={}, event={})
    assert snapshot(store.con) == before


def test_a_failure_midway_rolls_everything_back(store: Store, two) -> None:
    a, _, _, _ = two
    store.con.execute("CREATE TEMP TRIGGER boom BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT, 'boom'); END")
    before = snapshot(store.con)
    with pytest.raises(sqlite3.Error, match="boom"):
        delete(store, a)  # deliveries, batches and events went first
    assert not store.con.in_transaction
    assert snapshot(store.con) == before
    assert store.recent_events(kinds=["room_delete"]) == []


def test_a_side_effect_on_another_table_rolls_back(store: Store, two) -> None:
    """The per-table count check: a delete that touches any other row is undone."""
    a, _, _, _ = two
    store.web_session_create("s" * 64, 3600)
    store.con.execute("CREATE TEMP TRIGGER side AFTER DELETE ON rooms BEGIN DELETE FROM web_sessions; END")
    before = snapshot(store.con)
    with pytest.raises(StoreError, match="changed other rows"):
        delete(store, a)
    assert snapshot(store.con) == before


def test_a_broken_foreign_key_rolls_back(store: Store, two) -> None:
    """``PRAGMA foreign_key_check`` after the deletes (with foreign keys off, SQLite
    would not stop an orphan by itself)."""
    a, _, _, _ = two
    con = store.con
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("INSERT INTO messages(room_id, ts, sender_name, sender_kind, via, text)"
                " VALUES(9999, 0, 'x', 'system', 'system', 'orphan')")
    before = snapshot(con)
    with pytest.raises(StoreError, match="would leave rows that point at it"):
        delete(store, a)
    assert snapshot(con) == before

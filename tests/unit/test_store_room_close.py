"""store: closed rooms (DESIGN.md §28.2, §28.4, §28.5): the renamed row, kept credentials,
reopen, and the room references the human types."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeClock
from test_store import add_agent
from switchboard import db
from switchboard.models import InvalidName, Room, closed_room_name
from switchboard.store import Ambiguous, Conflict, NotFound, Store


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "y.db"), clock)


def room(store: Store, name: str) -> Room:
    return store.create_room(name, "alice", 60, 6)


def agent(store: Store, room_id: int, name: str, cred: str) -> int:
    """An active membership with its own credential hash; returns its id."""
    _, mid = add_agent(store, room_id, name)
    with db.tx(store.con):
        store.con.execute("UPDATE memberships SET cred_hash=? WHERE id=?", (cred, mid))
    return mid


def close(store: Store, r: Room) -> Room:
    """What ``/close`` does to the store (the service adds the messages): end every
    membership keeping its credential, record the event, rename the row."""
    with db.tx(store.con):
        ids = [m.membership_id for m in store.members(r.id)]
        for mid in ids:
            store.end_membership(mid, "closed", keep_cred=True)
        store.add_event("room_close", room_id=r.id,
                        data={"name": r.name, "closed_name": closed_room_name(r.name, r.id), "by": "alice",
                              "via": "web", "chain": None, "members": ids})
        return store.rename_room(r.id, closed_room_name(r.name, r.id), expect=r.name)


def membership(store: Store, mid: int) -> dict:
    row = store.con.execute("SELECT * FROM memberships WHERE id=?", (mid,)).fetchone()
    return {k: row[k] for k in row.keys()}


def test_list_rooms_hides_closed_rooms(store: Store, clock: FakeClock) -> None:
    b, a = room(store, "#build"), room(store, "#alpha")
    room(store, "#zeta")
    assert store.count_closed_rooms() == 0 and store.list_rooms(closed=True) == []
    cb = close(store, b)
    clock.advance(1)
    ca = close(store, a)
    assert (cb.name, ca.name) == (f"#build~closed-{b.id}", f"#alpha~closed-{a.id}")
    assert cb.closed and cb.display_name == "#build" and cb.id == b.id
    assert [r.name for r in store.list_rooms()] == ["#zeta"]
    assert [r.name for r in store.list_rooms(closed=True)] == [ca.name, cb.name]  # newest (highest id) first
    assert store.count_closed_rooms() == 2
    # the name is free again, and the new room is an open one
    nb = room(store, "#build")
    assert nb.id != b.id and not nb.closed
    assert [r.name for r in store.list_rooms()] == ["#build", "#zeta"]
    assert store.count_closed_rooms() == 2
    assert [r.id for r in store.closed_rooms("#build")] == [b.id]


def test_closed_rooms_matches_the_base_exactly(store: Store) -> None:
    """``substr``, not LIKE: '_' in ``#a_b`` is no wildcard; ``#a_bc`` is another room."""
    axb, a_bc, a_b = room(store, "#axb"), room(store, "#a_bc"), room(store, "#a_b")
    close(store, axb)
    close(store, a_bc)
    assert store.closed_rooms("#a_b") == []
    close(store, a_b)
    assert [r.name for r in store.closed_rooms("#a_b")] == [f"#a_b~closed-{a_b.id}"]
    assert [r.name for r in store.closed_rooms("#axb")] == [f"#axb~closed-{axb.id}"]
    # a row that only looks like one (never written by the broker) is not a closed room
    with db.tx(store.con):
        store.con.execute("UPDATE rooms SET name='#a_b~closed-x' WHERE id=?", (axb.id,))
    assert [r.id for r in store.closed_rooms("#a_b")] == [a_b.id]


def test_end_membership_keep_cred(store: Store) -> None:
    r = room(store, "#build")
    keep = agent(store, r.id, "alpha", "a" * 64)
    plain = agent(store, r.id, "beta", "b" * 64)
    msg = store.insert_message(r.id, sender_name="alice", sender_kind="human", via="web", text="hi")
    offered = store.create_batch(keep, path="wait", kind="wake", items=[(msg.id, 1)])
    store.end_membership(keep, "closed", keep_cred=True)
    m = membership(store, keep)
    assert m["cred_hash"] == "a" * 64 and m["left_reason"] == "closed" and m["kicked"] == 0
    assert m["left_at"] is not None
    assert store.delivery_state(keep, msg.id) == "revoked"
    b = store.get_batch(offered.id)
    assert b is not None and b.state == "cancelled" and b.expire_reason == "closed"
    # the kept hash never authorizes
    assert store.membership_by_cred("a" * 64) is None
    assert store.closed_membership_by_cred("a" * 64) is None  # the room isn't closed (yet)
    # a plain end still clears the hash
    store.end_membership(plain, "leave")
    assert membership(store, plain)["cred_hash"] is None
    # ending twice changes nothing
    store.end_membership(keep, "kick", kicked=True)
    assert membership(store, keep) == m


def test_closed_membership_by_cred(store: Store) -> None:
    r = room(store, "#build")
    mid = agent(store, r.id, "alpha", "a" * 64)
    assert store.membership_by_cred("a" * 64) is not None
    assert store.closed_membership_by_cred("a" * 64) is None  # active: not a closed one
    closed = close(store, r)
    got = store.closed_membership_by_cred("a" * 64)
    assert got is not None
    m, cr = got
    assert m.id == mid and m.cred_hash == "a" * 64 and not m.active and cr == closed
    assert store.membership_by_cred("a" * 64) is None
    assert store.closed_membership_by_cred("f" * 64) is None
    # once the room is reopened, the hash is gone and so is the hint
    store.reopen_room(r.id)
    assert store.closed_membership_by_cred("a" * 64) is None
    assert membership(store, mid)["cred_hash"] is None


def test_closed_membership_needs_the_room_still_closed(store: Store) -> None:
    """A kept hash whose room was renamed back some other way names nothing."""
    r = room(store, "#build")
    agent(store, r.id, "alpha", "a" * 64)
    c = close(store, r)
    store.rename_room(r.id, "#build", expect=c.name)
    assert store.closed_membership_by_cred("a" * 64) is None


def test_rename_room(store: Store) -> None:
    r = room(store, "#build")
    other = room(store, "#other")
    with pytest.raises(Conflict):
        store.rename_room(r.id, closed_room_name(r.name, r.id), expect="#wrong")
    with pytest.raises(Conflict):
        store.rename_room(9999, "#x~closed-9999", expect="#x")
    with pytest.raises(Conflict, match="#other already exists"):
        store.rename_room(r.id, "#other", expect="#build")
    for bad in ["#Build", "build", "#build~closed-0", "#build\n"]:
        with pytest.raises(ValueError):
            store.rename_room(r.id, bad, expect="#build")
    assert [x.name for x in store.list_rooms()] == ["#build", "#other"] and other.name == "#other"
    got = store.rename_room(r.id, "#renamed", expect="#build")
    assert got.name == "#renamed" and got.id == r.id


def test_reopen_room(store: Store, clock: FakeClock) -> None:
    r = store.set_paused(room(store, "#build").id, True, "paused by alice")
    alpha = agent(store, r.id, "alpha", "a" * 64)
    beta = agent(store, r.id, "beta", "b" * 64)
    store.end_membership(beta, "kick", kicked=True)
    store.insert_message(r.id, sender_name="alice", sender_kind="human", via="web", text="kept")
    c = close(store, r)
    with pytest.raises(NotFound, match="no closed room with id"):
        store.reopen_room(9999)
    other = room(store, "#other")
    with pytest.raises(NotFound):
        store.reopen_room(other.id)  # an open room
    new = room(store, "#build")
    with pytest.raises(Conflict, match="#build is taken by an open room"):
        store.reopen_room(c.id)
    assert store.room_by_id(c.id) == c and membership(store, alpha)["cred_hash"] == "a" * 64  # unchanged
    close(store, new)
    got = store.reopen_room(c.id)
    assert got.name == "#build" and got.id == r.id and not got.closed
    assert got.paused and got.paused_reason == "paused by alice"  # settings kept
    assert [m.text for m in store.history(r.id)] == ["kept"]
    assert store.members(r.id) == []  # nobody is re-added
    assert membership(store, alpha)["cred_hash"] is None
    assert store.was_kicked(r.id, store.get_membership(beta).participant_id)  # a kick still holds
    assert not store.was_kicked(r.id, store.get_membership(alpha).participant_id)
    assert store.count_closed_rooms() == 1  # the other #build


def test_latest_close_event(store: Store, clock: FakeClock) -> None:
    r = room(store, "#build")
    assert store.latest_close_event(r.id) is None
    c = close(store, r)
    first = store.latest_close_event(r.id)
    assert first is not None and first.kind == "room_close" and first.data["closed_name"] == c.name
    store.reopen_room(r.id)
    clock.advance(5)
    close(store, store.room_by_id(r.id))
    e = store.latest_close_event(r.id)
    assert e is not None and e.id > first.id and e.ts == first.ts + 5 and e.data["by"] == "alice"


def test_resolve_room(store: Store) -> None:
    # nothing
    with pytest.raises(NotFound, match="no such room: #build"):
        store.resolve_room("#build")
    with pytest.raises(InvalidName):
        store.resolve_room("#bad name")
    with pytest.raises(InvalidName):
        store.resolve_room("#build~closed-0")
    # the only closed one, by its base name (any spelling) or its full name
    old = close(store, room(store, "#build"))
    assert store.resolve_room("#build") == old
    assert store.resolve_room(" BUILD ") == old
    assert store.resolve_room(old.name.upper()) == old
    with pytest.raises(NotFound, match=r"no such room: #build~closed-999"):
        store.resolve_room("#build~closed-999")
    # several: Ambiguous, names newest first
    newer = close(store, room(store, "#build"))
    with pytest.raises(Ambiguous) as ei:
        store.resolve_room("#build")
    assert ei.value.names == [newer.name, old.name]
    assert str(ei.value) == f"#build names 2 closed rooms: {newer.name}, {old.name}"
    assert store.resolve_room(old.name) == old  # the full name still picks one
    # an open room wins over closed ones of its name
    live = room(store, "#build")
    assert store.resolve_room("#build") == live
    assert store.resolve_room(newer.name) == newer

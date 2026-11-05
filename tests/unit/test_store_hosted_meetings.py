"""store: calls hosted on a person's machine, and the lines pushed into them (#623, DESIGN.md §52.12)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.store import Conflict, Store

H1, H2 = "ab" * 32, "cd" * 32


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "m.db"), clock)


def token(store: Store, device: str = "alice-laptop", person: int | None = None) -> int:
    """A live agent token for ``device``: a hosted call names the token that opened it (a foreign key)."""
    row = store.agent_token_by_hash(hashlib.sha256(device.encode()).hexdigest()) or store.agent_token_add(
        person, device, hashlib.sha256(device.encode()).hexdigest()
    )
    assert row is not None
    return row.id


def host(
    store: Store, room_id: int, key: str = H1, *, person: int | None = None, token_id: int | None = None
) -> Any:
    return store.insert_hosted_meeting(
        room_id,
        started_by="alice",
        platform="unknown",
        key_hash=key,
        token_id=token_id or token(store),
        owner_person_id=person,
        host="alice-laptop",
        model="large-v3-turbo",
    )


def test_a_hosted_call_keeps_its_machine_owner_key_and_model_and_counts_with_the_others(store: Store) -> None:
    """The row says whose machine it is, which key (its hash only) and what model the host claimed, starts
    ``joining`` and counts toward ``max_calls`` with the broker's own bot's calls. Fails if a field is lost, a
    hosted call is not counted, or it reads as a server call."""
    room = store.create_room("#build", "alice", 60, 30)
    tok = token(store)
    row = host(store, room.id, token_id=tok)
    assert (row.source, row.state, row.platform, row.host, row.model) == (
        "hosted",
        "joining",
        "unknown",
        "alice-laptop",
        "large-v3-turbo",
    )
    assert (row.token_id, row.owner_person_id, row.key_hash, row.lines_pushed) == (tok, None, H1, 0)
    assert row.hosted is True and store.count_open_meetings() == 1
    other = store.create_room("#uploads", "alice", 60, 30)
    own = store.insert_meeting(other.id, started_by="alice", platform="zoom")
    assert (own.source, own.hosted, own.key_hash, own.host, own.token_id) == (
        "server",
        False,
        None,
        None,
        None,
    )
    assert store.count_open_meetings() == 2


def test_a_room_has_one_call_whatever_its_source_and_a_key_names_one_call_for_ever(store: Store) -> None:
    """The unique index on a room's open call holds across sources, and the one on the key holds across every
    call ever made: an ended call's key can't be given to another. Fails if either is missing."""
    a = store.create_room("#a", "alice", 60, 30)
    b = store.create_room("#b", "alice", 60, 30)
    first = host(store, a.id, H1)
    with pytest.raises(Conflict):
        host(store, a.id, H2)  # the room has a call
    with pytest.raises(Conflict):
        store.insert_meeting(a.id, started_by="alice", platform="zoom")  # nor may the bot start one beside it
    with pytest.raises(Conflict):
        host(store, b.id, H1)  # the key is taken
    store.end_meeting(first.id, None)
    with pytest.raises(Conflict):
        host(store, b.id, H1)  # still taken after the call ended
    assert store.meeting_by_key(H1).id == first.id
    assert store.meeting_by_key(H2) is None
    host(store, a.id, H2)  # the room is free again, with a new key


def test_a_persons_open_hosted_call_is_found_by_owner_and_the_owner_is_no_one_elses(store: Store) -> None:
    """``owner_person_id`` None is the broker's owner: asking for a person's call never finds the owner's, and
    the owner's is found with None. Fails if NULL is compared with ``=`` (which matches nothing) or the owner
    is mistaken for a person."""
    bob = store.person_add("bob", b"b" * 16, "x" * 64, 3600.0).id
    a = store.create_room("#a", "alice", 60, 30)
    b = store.create_room("#b", "alice", 60, 30)
    mine = host(store, a.id, H1, person=None, token_id=token(store))
    his = host(store, b.id, H2, person=bob, token_id=token(store, "bobs-laptop", bob))
    assert store.open_hosted_meeting_of(None).id == mine.id
    assert store.open_hosted_meeting_of(bob).id == his.id
    assert store.open_hosted_meeting_of(bob + 1) is None
    store.end_meeting(mine.id, None)
    assert store.open_hosted_meeting_of(None) is None
    assert [m.id for m in store.open_hosted_meetings()] == [his.id]
    own = store.insert_meeting(a.id, started_by="alice", platform="zoom")
    assert store.open_hosted_meeting_of(None) is None  # the broker's own call is nobody's hosted call
    assert own.id not in [m.id for m in store.open_hosted_meetings()]


def test_a_line_stored_with_a_call_is_counted_by_that_call_and_no_other_is(store: Store) -> None:
    """``lines_pushed`` goes up in the same transaction as the line that names the call, and only then:
    chat, notices and a bot's own lines leave it alone. Fails if the count is kept elsewhere, drifts, or a
    line is stored without its call."""
    a = store.create_room("#a", "alice", 60, 30)
    call = host(store, a.id)
    kw = {"sender_name": "bob", "sender_kind": "meeting", "via": "meeting"}
    one = store.insert_message(a.id, text="one", meeting_id=call.id, sender_host="alice-laptop", **kw)
    store.insert_message(a.id, text="two", meeting_id=call.id, sender_host="alice-laptop", **kw)
    store.insert_message(a.id, text="from the bot", **kw)
    store.insert_message(a.id, text="chat", sender_name="alice", sender_kind="human", via="web")
    assert store.meeting_by_id(call.id).lines_pushed == 2
    row = store.con.execute("SELECT meeting_id, sender_host FROM messages WHERE id=?", (one.id,)).fetchone()
    assert tuple(row) == (call.id, "alice-laptop")
    with pytest.raises(Exception, match="FOREIGN KEY"):
        store.insert_message(a.id, text="x", meeting_id=999, **kw)


def test_a_token_is_found_by_id_whether_or_not_it_was_revoked(store: Store) -> None:
    """A call names the token that opened it; the row is read after the token is revoked, to tell that it was.
    Fails if a revoked token cannot be read."""
    tid = token(store)
    assert store.agent_token_by_id(tid).revoked_at is None
    store.agent_token_revoke(tid, None)
    assert store.agent_token_by_id(tid).revoked_at is not None
    assert store.agent_token_by_id(tid + 99) is None

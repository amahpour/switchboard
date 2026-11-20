"""Schema 54 (#843, DESIGN.md §63): public rooms have members, in ``room_people`` like a private room's
people. The step from 53 only adds ``joined_with``; the day-one members are filled once, at the broker's
first start, from what a real old database recorded (``Store.join_day_one``)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.store import Store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def columns(con: sqlite3.Connection) -> list[tuple[object, ...]]:
    """``room_people``'s columns as SQLite sees them: name, type, not null, default, key."""
    return [tuple(r)[1:] for r in con.execute("PRAGMA table_info(room_people)")]


def at_53(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The 0.37.2 dump (schema 28) migrated to 53, with what a hosted broker of today records about bob
    (person 1): he opened #bench (a seen mark), his agent on his device ``desktop`` was in #build, and a
    person since removed posted in #build. The dump already has the owner's posts in #build and #bench,
    an agent of the owner's in the closed #old, and bob and the owner in the private #secret."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    try:
        old.executescript(FIXTURE.read_text())
    finally:
        old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 53)
        con = db.open_db(path)
        with db.tx(con):
            con.execute("INSERT INTO home_seen(person_key, room_id, seen_id) VALUES(1, 2, 17)")
            con.execute(
                "INSERT INTO participants(id, harness, session_key, created_at, host)"
                " VALUES(20, 'codex', 'codex@desktop:t1', 1790000020.0, 'desktop')"
            )
            con.execute(
                "INSERT INTO memberships(room_id, participant_id, screen_name, joined_at, join_msg_id)"
                " VALUES(1, 20, 'codex-2', 1790000020.0, 0)"
            )
            con.execute(
                "INSERT INTO people(id, name, handle, created_at, removed_at)"
                " VALUES(2, 'carol', X'00112233445566778899AABBCCDDEEFF', 1790000001.0, 1790000030.0)"
            )
            con.execute(
                "INSERT INTO messages(room_id, ts, sender_name, sender_kind, via, kind, text, mentions,"
                " sender_person_id) VALUES(1, 1790000025.0, 'carol', 'human', 'web', 'chat', 'hi', '[]', 2)"
            )
        con.close()
    return path


def test_the_step_adds_only_the_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """From 53, a checked backup, every row kept, and ``room_people`` shaped like a fresh database's.
    Fails if the step drops or changes a row, or the column differs from the fresh schema."""
    path = at_53(tmp_path, monkeypatch)
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 53)
        before = db.open_db(path)
        counts = db.row_counts(before, db.tables_of(53))
        before.close()
    upgraded = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    backup = db.connect(path.with_name("old.db.v53.bak"), readonly=True)
    try:
        assert db.schema_version(upgraded) == db.SCHEMA_VERSION == 57
        assert db.schema_version(backup) == 53
        assert db.row_counts(upgraded, db.TABLES) == {**counts, "mcp_sessions": 0}  # 57's table, empty
        assert columns(upgraded) == columns(fresh)
        assert ("joined_with", "TEXT", 0, None, 0) in columns(upgraded)
        assert all(r[0] is None for r in upgraded.execute("SELECT joined_with FROM room_people"))
        assert db.integrity_ok(upgraded) is None
        with pytest.raises(sqlite3.IntegrityError):  # the CHECK came with it
            upgraded.execute("UPDATE room_people SET joined_with='' WHERE room_id=4")
    finally:
        backup.close()
        upgraded.close()
        fresh.close()


def test_day_one_joins_each_person_to_the_rooms_they_used_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each person joins every public room they opened, posted in, or had an agent in, and nothing else:
    the owner #build and #bench (posts) and the closed #old (an agent on this machine), bob #bench (opened)
    and #build (his device's agent), carol nothing (removed), and #secret keeps its own people. Run again,
    and after bob left #bench, it adds nothing. Fails if a sidebar would empty on day one, if a removed
    person or a private room is joined, or if a restart undoes someone's Leave."""
    path = at_53(tmp_path, monkeypatch)
    store = Store(db.open_db(path), FakeClock())
    secret = store.room_people(4)
    assert store.join_day_one() == 5
    assert {r: store.room_people(r) for r in (1, 2, 3, 4)} == {
        1: [0, 1],
        2: [0, 1],
        3: [0],
        4: secret,
    }
    assert store.rooms_of_person(2) == []
    assert store.person_rooms(1) == {1: None, 2: None, 4: None}
    assert store.room_person_remove(2, 1)
    assert store.join_day_one() == 0
    assert store.room_people(2) == [0]
    assert store.meta_get(Store.DAY_ONE_KEY) is not None


def test_day_one_gives_an_agent_to_whoever_held_its_device_when_it_joined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device name is unique only among live tokens, so it can pass to someone else after a revoke: an
    old agent is its person's by the token its device had when the agent joined, not the one it has today.
    bob's agent on ``shared`` joined #lab under bob's token, and dave holds ``shared`` now: bob joins #lab,
    dave nothing. In #lab2, an agent on ``gone`` (a token of carol's, since revoked, and carol removed)
    and one on ``never`` (no token ever) belong to nobody known, so nobody joins, the owner included.
    Fails if day one reads today's token, or gives an agent it cannot place to the owner."""
    store = Store(db.open_db(at_53(tmp_path, monkeypatch)), FakeClock())
    lab = store.create_room("#lab", "alice", 60, 6)
    lab2 = store.create_room("#lab2", "alice", 60, 6)
    with db.tx(store.con):
        store.con.execute(
            "INSERT INTO people(id, name, handle, created_at)"
            " VALUES(3, 'dave', X'FFEEDDCCBBAA99887766554433221101', 1790000001.0)"
        )
        for tid, person, device, made, revoked in (
            (10, 1, "shared", 1790000040.0, 1790000060.0),
            (11, 3, "shared", 1790000070.0, None),
            (12, 2, "gone", 1790000040.0, 1790000060.0),
        ):
            store.con.execute(
                "INSERT INTO agent_tokens(id, person_id, device, token_hash, created_at, revoked_at)"
                " VALUES(?,?,?,?,?,?)",
                (tid, person, device, f"{tid:064d}", made, revoked),
            )
        for pid, host, room in ((30, "shared", lab.id), (31, "gone", lab2.id), (32, "never", lab2.id)):
            store.con.execute(
                "INSERT INTO participants(id, harness, session_key, created_at, host)"
                " VALUES(?, 'codex', ?, 1790000050.0, ?)",
                (pid, f"codex@{host}:t{pid}", host),
            )
            store.con.execute(
                "INSERT INTO memberships(room_id, participant_id, screen_name, joined_at, join_msg_id)"
                " VALUES(?, ?, ?, 1790000050.0, 0)",
                (room, pid, f"codex-{pid}"),
            )
    store.join_day_one()
    assert store.room_people(lab.id) == [1]
    assert store.room_people(lab2.id) == []


def test_a_fresh_database_has_nothing_to_join(tmp_path: Path) -> None:
    """A new home's first start finds no rooms, marks the day done and joins nobody; a room made since is
    joined by its creator only. Fails if day one ever runs over rows made after it."""
    store = Store(db.open_db(tmp_path / "new.db"), FakeClock())
    assert store.join_day_one() == 0
    room = store.create_room("#build", "alice", 60, 6, member=0)
    store.insert_message(
        room.id, sender_name="bob", sender_kind="human", via="web", text="hi", sender_person_id=7
    )
    assert store.join_day_one() == 0
    assert store.room_people(room.id) == [0]

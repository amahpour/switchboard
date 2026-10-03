"""The third schema migration, v3 -> v4 (DESIGN.md §32.2), on a database made by 0.7.0.

The fixture ``tests/fixtures/db/v0_7_0.sql`` is a dump of a database written by
switchboard 0.7.0's own store and engine (``make_v0_7_0.py``): every schema-3 table has
rows, the owner has claimed it, and a machine that dials in has a member in a room.
The migration uses the same machinery as before: a verified 0600 backup
(``switchboard.db.v3.bak``), every statement in one transaction, then the checks. It
adds people, and a person column (NULL: the owner) to passkeys, web
sessions, machines and messages, so every existing row stays the owner's and none is
rewritten.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from switchboard import db
from switchboard.store import Store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"
V3_TABLES = ("meta", "rooms", "participants", "memberships", "messages", "batches", "deliveries", "events",
             "web_sessions", "remotes", "passkeys", "link_machines")
NEW_TABLES = ("people",)
PERSON_COLUMNS = {"messages": "sender_person_id", "web_sessions": "person_id", "passkeys": "person_id",
                  "link_machines": "person_id"}


def make_v3(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text(encoding="utf-8"))
    con.close()
    os.chmod(path, 0o600)
    return dump(path)


def dump(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return list(con.iterdump())
    finally:
        con.close()


def version(path: Path) -> int | None:
    con = sqlite3.connect(path)
    try:
        return db.schema_version(con)
    finally:
        con.close()


def columns(con: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(r) for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".v3.bak*"))


def test_fixture_is_a_v3_database_with_rows_in_every_table(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    make_v3(p)
    con = sqlite3.connect(p)
    assert db.schema_version(con) == 3
    assert all(db.row_counts(con, V3_TABLES).values())
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables - {"sqlite_sequence"} == set(V3_TABLES)
    for t, col in PERSON_COLUMNS.items():
        assert col not in {c[1] for c in columns(con, t)}
    assert con.execute("SELECT value FROM meta WHERE key='owner_handle'").fetchone() is not None
    con.close()


def test_fresh_db_keeps_the_v4_person_columns(tmp_path: Path) -> None:
    con = db.open_db(tmp_path / "switchboard.db")
    assert db.schema_version(con) == db.SCHEMA_VERSION == 5
    for t, col in PERSON_COLUMNS.items():
        c = {x[1]: x for x in columns(con, t)}[col]
        assert c[2] == "INTEGER" and c[3] == 0 and c[4] is None  # nullable, no default: NULL is the owner
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(NEW_TABLES) <= tables and tables == set(db.TABLES) | {"sqlite_sequence"}
    assert db.tables_of(3) == db.V3_TABLES and db.tables_of(4) == db.V4_TABLES
    assert backups(tmp_path / "switchboard.db") == []


def test_v3_fixture_migrates_to_v4(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v3(p)
    src = sqlite3.connect(p)
    before = db.row_counts(src, V3_TABLES)
    src.close()
    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION
    assert db.integrity_ok(con) is None
    assert db.row_counts(con, V3_TABLES) == before
    assert db.row_counts(con, NEW_TABLES) == {"people": 0}
    # the migrated schema is the fresh one: same columns in the same order, same indexes
    fresh = db.open_db(tmp_path / "fresh.db")
    for t in db.TABLES:
        assert columns(con, t) == columns(fresh, t), t
    q = "SELECT name, tbl_name FROM sqlite_master WHERE type='index' ORDER BY name"
    assert con.execute(q).fetchall() == fresh.execute(q).fetchall()
    # nothing was rewritten: every person column is NULL, so every row is still the owner's
    for t, col in PERSON_COLUMNS.items():
        assert con.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} IS NOT NULL").fetchone()[0] == 0, t
    # the backup: the version-3 file exactly as it was, 0600
    [bak] = backups(p)
    assert bak.name == "switchboard.db.v3.bak" and (os.stat(bak).st_mode & 0o777) == 0o600
    assert dump(bak) == original and version(bak) == 3
    # and the store works on it: the owner, their passkeys and sessions, the machines
    store = Store(con)
    assert store.owner_handle() == bytes.fromhex("00112233445566778899aabbccddeeff")
    assert store.passkey_count() == 2 and store.web_session_count() == 3
    assert {m.name for m in store.machines()} == {"work-laptop", "lab-box"}
    room = store.get_room("#build")
    assert room is not None
    assert {m.name: m.host for m in store.members(room.id)}["laptop-1"] == "work-laptop"


def test_failed_migration_leaves_v3_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v3(p)
    bad = list(db.V3_TO_V4)
    bad[4] = "CREATE TABLE people(id INTEGER REFERENCES no_such_table(x)) STRICT NONSENSE"  # after the ALTERs
    monkeypatch.setattr(db, "V3_TO_V4", tuple(bad))
    with pytest.raises(db.SchemaError, match="unchanged, still schema version 3; backup: switchboard.db.v3.bak"):
        db.open_db(p)
    assert version(p) == 3 and dump(p) == original  # the ALTERs were rolled back too
    monkeypatch.undo()
    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION
    assert len(backups(p)) == 2


def test_the_v2_fixture_takes_both_steps_after_one_backup(tmp_path: Path) -> None:
    from test_db_migrate_v3 import make_v2

    p = tmp_path / "switchboard.db"
    original = make_v2(p)
    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION
    assert db.row_counts(con, NEW_TABLES) == {"people": 0}
    con.close()
    names = sorted(x.name for x in tmp_path.glob("switchboard.db.*.bak*"))
    assert names == ["switchboard.db.v2.bak"]
    assert dump(tmp_path / "switchboard.db.v2.bak") == original

"""The second schema migration, v2 -> v3 (DESIGN.md §31.2), on a database made by 0.6.5.

The fixture ``tests/fixtures/db/v0_6_5.sql`` is a dump of a database written by
switchboard 0.6.5's own store and engine (see its README): every schema-2 table has
rows, one member is on a remote host and its ``remotes`` row holds the owner's consent.
The migration uses the v1 -> v2 machinery as it is: a verified 0600 backup of the
version-2 file (``switchboard.db.v2.bak``), then every statement in one transaction,
then the checks; a failure leaves the version-2 database untouched.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from switchboard import db
from switchboard.store import Store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_6_5.sql"
V2_TABLES = (
    "meta",
    "rooms",
    "participants",
    "memberships",
    "messages",
    "batches",
    "deliveries",
    "events",
    "web_sessions",
    "remotes",
)
NEW_TABLES = ("passkeys",)  # and link_machines, which schema 29 dropped again (#385)


def make_v2(path: Path) -> list[str]:
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
    return sorted(path.parent.glob(path.name + ".v2.bak*"))


def test_fixture_is_a_v2_database_with_rows_in_every_table(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    make_v2(p)
    con = sqlite3.connect(p)
    assert db.schema_version(con) == 2
    assert all(db.row_counts(con, V2_TABLES).values())
    assert "via" not in {c[1] for c in columns(con, "web_sessions")}
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables - {"sqlite_sequence"} == set(V2_TABLES)
    assert con.execute("SELECT COUNT(*) FROM participants WHERE host='fpga-pi'").fetchone()[0] == 1
    con.close()


def test_a_fresh_db_has_what_v3_added(tmp_path: Path) -> None:
    con = db.open_db(tmp_path / "switchboard.db")
    assert db.schema_version(con) == db.SCHEMA_VERSION >= 3  # v4 since #61 (§32.2)
    via = {c[1]: c for c in columns(con, "web_sessions")}["via"]
    assert via[2] == "TEXT" and via[3] == 0  # nullable: a session made before schema 3 has none
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(NEW_TABLES) <= tables and tables == set(db.TABLES) | {"sqlite_sequence"}
    assert (
        db.kept_tables(1) == db.kept(db.V1_TABLES)
        and db.kept_tables(2) == db.kept(db.V2_TABLES)
        and db.kept_tables(3) == db.kept(db.V3_TABLES)
    )
    assert backups(tmp_path / "switchboard.db") == []


def test_v2_fixture_migrates_through_v3(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v2(p)
    src = sqlite3.connect(p)
    before = db.row_counts(src, db.kept(V2_TABLES))
    src.close()
    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION
    assert db.integrity_ok(con) is None
    assert db.row_counts(con, db.kept(V2_TABLES)) == before
    assert db.row_counts(con, NEW_TABLES) == {"passkeys": 0}
    # the migrated schema is the fresh one: same columns in the same order, same indexes
    fresh = db.open_db(tmp_path / "fresh.db")
    for t in db.TABLES:
        assert columns(con, t) == columns(fresh, t), t
    q = "SELECT name, tbl_name FROM sqlite_master WHERE type='index' ORDER BY name"
    assert con.execute(q).fetchall() == fresh.execute(q).fetchall()
    # the backup: the version-2 file exactly as it was, 0600, one self-contained file
    [bak] = backups(p)
    assert bak.name == "switchboard.db.v2.bak" and (os.stat(bak).st_mode & 0o777) == 0o600
    assert dump(bak) == original and version(bak) == 2
    assert not any(tmp_path.glob("switchboard.db.v1.bak*"))  # a version-2 file needs no v1 backup
    # and the store works on it: the 0.6.5 rows are readable. The remote member kept its host and its
    # messages, and both it and the local session were ended with the paths they came by (schemas 29
    # and 30, #385): their rows stay, and nobody is active
    store = Store(con)
    room = store.get_room("#build")
    assert room is not None
    assert store.members(room.id) == []
    assert con.execute("SELECT COUNT(*) FROM participants WHERE ended_at IS NULL").fetchone()[0] == 0
    assert con.execute("SELECT host, ended_at IS NOT NULL FROM participants WHERE host='fpga-pi'").fetchall()
    assert [m.sender_host for m in store.history(room.id) if m.sender_name == "bench"] == [
        "fpga-pi",
        "fpga-pi",
    ]
    # the old web session has no `via`; it is still a session
    assert store.web_session_count() == 1
    assert con.execute("SELECT via FROM web_sessions").fetchone()[0] is None
    assert store.owner_handle() is None and store.passkey_count() == 0


def test_the_backup_is_written_before_the_first_statement(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v2(p)
    bak = db.backup_path_for(p, 2)
    assert bak == tmp_path / "switchboard.db.v2.bak"
    seen: list[tuple[str, bool]] = []
    con = db.connect(p)
    con.set_trace_callback(lambda sql: seen.append((sql, bak.exists())))
    db.migrate(con, backup_to=bak)
    con.set_trace_callback(None)
    con.close()
    alters = [ok for sql, ok in seen if sql.lstrip().upper().startswith(("ALTER", "CREATE TABLE"))]
    # ALTERs and CREATEs per step, v2 to v32 (29 drops two tables and alters none; 30 drops thirteen columns;
    # 31 adds two columns and a table; 32 adds a table)
    per_step = [3, 5, 1, 1, 2, 2, 2, 1, 1, 1, 2, 2, 2, 1, 2, 1, 2, 1, 1, 1, 1, 5, 1, 1, 1, 3, 13, 3, 1]
    assert len(alters) == sum(per_step) and all(alters)
    assert dump(bak) == original
    c = sqlite3.connect(bak)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    c.close()


def test_failed_migration_leaves_v2_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v2(p)
    bad = list(db.V2_TO_V3)
    bad[2] = (
        "CREATE TABLE link_machines(name TEXT REFERENCES no_such_table(x)) STRICT NONSENSE"  # after the ALTER
    )
    monkeypatch.setattr(db, "V2_TO_V3", tuple(bad))
    with pytest.raises(
        db.SchemaError, match="unchanged, still schema version 2; backup: switchboard.db.v2.bak"
    ):
        db.open_db(p)
    assert version(p) == 2
    assert dump(p) == original  # the ALTER and the passkeys table were rolled back too
    [bak] = backups(p)
    assert dump(bak) == original
    monkeypatch.undo()
    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION
    assert len(backups(p)) == 2


def test_a_step_that_forgets_its_version_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v2(p)
    monkeypatch.setattr(db, "V2_TO_V3", tuple(db.V2_TO_V3[:-1]))
    with pytest.raises(db.SchemaError, match="did not set schema version 3"):
        db.open_db(p)
    assert version(p) == 2 and dump(p) == original


def test_migration_needs_a_backup_path(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v2(p)
    con = db.connect(p)
    with pytest.raises(
        db.SchemaError, match="schema version 2 needs a migration to version 32, and a migration"
    ):
        db.migrate(con)
    con.close()
    assert dump(p) == original and backups(p) == []


def test_a_migrated_v2_reopens_without_migrating(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    make_v2(p)
    db.open_db(p).close()
    [bak] = backups(p)
    first = dump(p)
    seen: list[str] = []
    con = db.connect(p)
    con.set_trace_callback(seen.append)
    assert db.migrate(con, backup_to=db.backup_path_for(p, 2)) == db.SCHEMA_VERSION
    con.set_trace_callback(None)
    con.close()
    assert not [s for s in seen if s.lstrip().upper().startswith(("ALTER", "CREATE", "UPDATE", "INSERT"))]
    assert backups(p) == [bak] and dump(p) == first


def test_the_v1_fixture_takes_both_steps_after_one_backup(tmp_path: Path) -> None:
    from test_db_migrate_v2 import make_v1

    p = tmp_path / "switchboard.db"
    original = make_v1(p)
    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION
    assert db.row_counts(con, NEW_TABLES) == {"passkeys": 0}
    assert "via" in {c[1] for c in columns(con, "web_sessions")}
    con.close()
    names = sorted(x.name for x in tmp_path.glob("switchboard.db.*.bak*"))
    assert names == ["switchboard.db.v1.bak"]  # one backup, of the file as it was, before anything ran
    assert dump(tmp_path / "switchboard.db.v1.bak") == original

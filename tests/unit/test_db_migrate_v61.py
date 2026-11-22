"""Schema 61 admits Grok Build as a harness (DESIGN.md §49.15, #851). The step from 60 (#923's webhook tables)
only edits the CHECK on ``participants.harness``: no table, no row, no foreign key and no other object
changes. It runs on ``v1_14_0.sql``, a real schema-60 database 1.14.0 wrote (``tests/fixtures/db/README.md``)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v1_14_0.sql"
PARTICIPANTS = "SELECT sql FROM sqlite_master WHERE type='table' AND name='participants'"
OTHERS = "SELECT type, name, sql FROM sqlite_master WHERE name!='participants' ORDER BY type, name"
INSERT = "INSERT INTO participants(harness,session_key,created_at) VALUES(?,?,1.0)"


def words(sql: str) -> str:
    return " ".join(sql.split())


def at_60(path: Path) -> None:
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())


def test_a_schema_60_database_admits_grok_and_nothing_else_changes(tmp_path: Path) -> None:
    """A real 1.14.0 database (schema 60, a member of every harness it knew, a webhook) refuses a Grok member; after the step it takes one, with the CHECK a
    fresh database has, and still refuses a harness nobody knows. Every other object's SQL, every row and
    every foreign key is as it was, and the backup is schema 60 without Grok. Fails if the CHECK is only
    edited in the fresh schema, the step edits something else, or it loosens the CHECK to anything."""
    path = tmp_path / "old.db"
    at_60(path)
    with sqlite3.connect(path) as before:
        assert db.schema_version(before) == 60
        with pytest.raises(sqlite3.IntegrityError):
            before.execute(INSERT, ("grok", "grok@laptop:s1"))
        before.rollback()
        counts = db.row_counts(before, db.TABLES)
        others = [tuple(r) for r in before.execute(OTHERS)]
    con = db.open_db(path)
    backup = db.connect(db.backup_path_for(path, 60), readonly=True)
    fresh = db.open_db(tmp_path / "fresh.db")
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION == 61
        assert db.schema_version(backup) == 60
        assert "'grok'" not in backup.execute(PARTICIPANTS).fetchone()[0]
        # the same table, spaced as each was written: SQLite keeps a table's text as it was given
        migrated, made = (c.execute(PARTICIPANTS).fetchone()[0] for c in (con, fresh))
        assert words(migrated) == words(made)
        assert db.row_counts(con, db.TABLES) == counts
        assert [tuple(r) for r in con.execute(OTHERS)] == others
        assert list(con.execute("PRAGMA foreign_key_check")) == []
        assert con.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'green'").fetchone()[0] == 2
        assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
        con.execute(INSERT, ("grok", "grok@laptop:s1"))
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(INSERT, ("not-a-harness", "x@laptop:s1"))
        assert db.integrity_ok(con) is None
    finally:
        fresh.close()
        backup.close()
        con.close()


def test_an_unmatched_check_fails_closed_at_60(tmp_path: Path) -> None:
    """A CHECK spelled otherwise than the step's needle can't be skipped silently: the step fails, the
    database stays schema 60 with its CHECK as it was and normal schema protections on. Fails if the step's
    required fragment isn't verified."""
    path = tmp_path / "old.db"
    at_60(path)
    con = db.connect(path)
    con.execute("PRAGMA writable_schema=ON")
    con.execute(
        "UPDATE sqlite_master SET sql=replace(sql, '''opencode'',''test''', '''opencode'', ''test''')"
        " WHERE type='table' AND name='participants'"
    )
    con.execute("PRAGMA writable_schema=RESET")
    before = con.execute(PARTICIPANTS).fetchone()[0]
    try:
        with pytest.raises(db.SchemaError, match="unchanged, still schema version 60"):
            db.migrate(con, backup_to=tmp_path / "before-61.db")
        assert db.schema_version(con) == 60
        assert con.execute(PARTICIPANTS).fetchone()[0] == before
        assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
        assert db.integrity_ok(con) is None
    finally:
        con.close()


def test_a_schema_61_database_opens_unchanged(tmp_path: Path) -> None:
    """A database at the current version is opened as it is: no step and no backup."""
    path = tmp_path / "now.db"
    con = db.open_db(path)
    schema = sorted(tuple(r) for r in con.execute("SELECT type, name, sql FROM sqlite_master"))
    con.close()
    again = db.open_db(path)
    try:
        assert db.schema_version(again) == db.SCHEMA_VERSION
        assert sorted(tuple(r) for r in again.execute("SELECT type, name, sql FROM sqlite_master")) == schema
        assert not list(tmp_path.glob("now.db.v*.bak*"))
    finally:
        again.close()

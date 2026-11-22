"""Schema 60 admits Grok Build as a harness (DESIGN.md §49.15, #851). The step from 59 only edits the CHECK
on ``participants.harness``: no table, no row, no foreign key and no other object changes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"
PARTICIPANTS = "SELECT sql FROM sqlite_master WHERE type='table' AND name='participants'"
OTHERS = "SELECT type, name, sql FROM sqlite_master WHERE name!='participants' ORDER BY type, name"
INSERT = "INSERT INTO participants(harness,session_key,created_at) VALUES(?,?,1.0)"


def words(sql: str) -> str:
    return " ".join(sql.split())


def at_59(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 59)
        db.open_db(path).close()


def test_a_schema_59_database_admits_grok_and_nothing_else_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real old database brought to 59 refuses a Grok member; after the step it takes one, with the CHECK a
    fresh database has, and still refuses a harness nobody knows. Every other object's SQL, every row and
    every foreign key is as it was, and the backup is schema 59 without Grok. Fails if the CHECK is only
    edited in the fresh schema, the step edits something else, or it loosens the CHECK to anything."""
    path = tmp_path / "old.db"
    at_59(path, monkeypatch)
    with sqlite3.connect(path) as before:
        assert db.schema_version(before) == 59
        with pytest.raises(sqlite3.IntegrityError):
            before.execute(INSERT, ("grok", "grok@laptop:s1"))
        before.rollback()
        counts = db.row_counts(before, db.TABLES)
        others = [tuple(r) for r in before.execute(OTHERS)]
    con = db.open_db(path)
    backup = db.connect(db.backup_path_for(path, 59), readonly=True)
    fresh = db.open_db(tmp_path / "fresh.db")
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION == 60
        assert db.schema_version(backup) == 59
        assert "'grok'" not in backup.execute(PARTICIPANTS).fetchone()[0]
        # the same table, spaced as each was written: SQLite keeps a table's text as it was given
        migrated, made = (c.execute(PARTICIPANTS).fetchone()[0] for c in (con, fresh))
        assert words(migrated) == words(made)
        assert db.row_counts(con, db.TABLES) == counts
        assert [tuple(r) for r in con.execute(OTHERS)] == others
        assert list(con.execute("PRAGMA foreign_key_check")) == []
        assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
        con.execute(INSERT, ("grok", "grok@laptop:s1"))
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(INSERT, ("not-a-harness", "x@laptop:s1"))
        assert db.integrity_ok(con) is None
    finally:
        fresh.close()
        backup.close()
        con.close()


def test_an_unmatched_check_fails_closed_at_59(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CHECK spelled otherwise than the step's needle can't be skipped silently: the step fails, the
    database stays schema 59 with its CHECK as it was and normal schema protections on. Fails if the step's
    required fragment isn't verified."""
    path = tmp_path / "old.db"
    at_59(path, monkeypatch)
    con = db.connect(path)
    con.execute("PRAGMA writable_schema=ON")
    con.execute(
        "UPDATE sqlite_master SET sql=replace(sql, '''opencode'',''test''', '''opencode'', ''test''')"
        " WHERE type='table' AND name='participants'"
    )
    con.execute("PRAGMA writable_schema=RESET")
    before = con.execute(PARTICIPANTS).fetchone()[0]
    try:
        with pytest.raises(db.SchemaError, match="unchanged, still schema version 59"):
            db.migrate(con, backup_to=tmp_path / "before-60.db")
        assert db.schema_version(con) == 59
        assert con.execute(PARTICIPANTS).fetchone()[0] == before
        assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
        assert db.integrity_ok(con) is None
    finally:
        con.close()


def test_a_schema_60_database_opens_unchanged(tmp_path: Path) -> None:
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

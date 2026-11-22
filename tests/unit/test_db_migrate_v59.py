"""Schema 59 indexes the chat lines that name someone or reply to one, for a room's Mentions filter (DESIGN.md
§64, #914). The step is from 58 (#903's ``mcp_sessions``) and only adds ``messages_addressed``: no table, no
row and no other object changes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"
INDEX = "SELECT name, tbl_name, sql FROM sqlite_master WHERE name='messages_addressed'"
OTHERS = "SELECT type, name, sql FROM sqlite_master WHERE name!='messages_addressed' ORDER BY type, name"


def at_58(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 58)
        db.open_db(path).close()


def test_a_schema_58_database_gains_the_index_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real old database brought to 58 has no ``messages_addressed``. After the step it has the index, with
    the SQL a fresh database has; every other object's SQL and every table's rows are as they were, and the
    backup is still schema 58 without it. Fails if the index is only in the fresh schema, the step starts
    from another version, or the migration drops or changes anything else."""
    path = tmp_path / "old.db"
    at_58(path, monkeypatch)
    with sqlite3.connect(path) as before:
        assert db.schema_version(before) == 58
        assert before.execute(INDEX).fetchone() is None
        counts = db.row_counts(before, db.tables_of(58))
        others = [tuple(r) for r in before.execute(OTHERS)]
        messages = [tuple(r) for r in before.execute("SELECT * FROM messages ORDER BY id")]
    with monkeypatch.context() as mp:
        # this step alone: 60 adds the webhook tables (#923), 61 edits the participants CHECK (#851)
        mp.setattr(db, "SCHEMA_VERSION", 59)
        db.open_db(path).close()
    con = db.connect(path)
    backup = db.connect(db.backup_path_for(path, 58), readonly=True)
    fresh = db.open_db(tmp_path / "fresh.db")
    try:
        assert db.schema_version(con) == 59
        assert db.schema_version(backup) == 58
        assert backup.execute(INDEX).fetchone() is None
        assert [tuple(r) for r in con.execute(INDEX)] == [tuple(r) for r in fresh.execute(INDEX)]
        assert con.execute(INDEX).fetchone() is not None
        assert db.row_counts(con, db.tables_of(59)) == counts
        assert [tuple(r) for r in con.execute(OTHERS)] == others
        assert [tuple(r) for r in con.execute("SELECT * FROM messages ORDER BY id")] == messages
        assert db.integrity_ok(con) is None
    finally:
        fresh.close()
        backup.close()
        con.close()


def test_a_schema_59_database_opens_unchanged(tmp_path: Path) -> None:
    """A database at the current version is opened as it is: no step and no backup. Fails if opening a 59
    database migrates or backs it up again."""
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

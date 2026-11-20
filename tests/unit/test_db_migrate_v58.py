"""Schema 58 keeps which client opened each MCP session, so a restart can open it again (DESIGN.md §49.2,
#823). The step is from 57 (#907's ``pr_ci`` CHECK edit, #910), so a schema 56 database goes through that
edit first, and a schema 57 one only gains the table."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_db_migrate_v56 import el
from test_db_migrate_v57 import INSERT, assert_seeded, at_56

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"
QUERY = "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='mcp_sessions' ORDER BY name"


def same_table_as_fresh(con: sqlite3.Connection, tmp_path: Path) -> None:
    fresh = db.open_db(tmp_path / "fresh.db")
    try:
        got = [tuple(r) for r in con.execute(QUERY)]
        assert got == [tuple(r) for r in fresh.execute(QUERY)]
        assert len(got) >= 2  # the table and its index
    finally:
        fresh.close()


def test_a_schema_56_database_takes_57s_check_edit_and_gains_mcp_sessions_at_58(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real schema 56 database goes through both steps in one upgrade: 57 widens the ``pr_ci`` CHECK (its
    reviews table reads as a fresh database's and stores "blocked") and 58 adds an empty table the same as a
    fresh database's, every other row kept. Fails if 58 skips 57 (as a step from 56 to 58 would) or the
    table differs."""
    path = tmp_path / "old.db"
    at_56(path, monkeypatch)
    with sqlite3.connect(path) as before:
        assert db.schema_version(before) == 56
        counts = db.row_counts(before, db.tables_of(56))
    con = db.open_db(path)
    backup = db.connect(db.backup_path_for(path, 56), readonly=True)
    fresh = db.open_db(tmp_path / "fresh.db")
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION == 58
        assert db.schema_version(backup) == 56
        assert db.row_counts(con, db.TABLES) == {**counts, "mcp_sessions": 0}
        same_table_as_fresh(con, tmp_path)
        assert not backup.execute(QUERY).fetchall()
        q = "SELECT sql FROM sqlite_master WHERE name='reviews'"
        assert con.execute(q).fetchone()[0] == fresh.execute(q).fetchone()[0]
        con.execute(INSERT, ("https://example.com/a/pull/1", "blocked"))
        assert_seeded(con)
        assert db.integrity_ok(con) is None
    finally:
        fresh.close()
        backup.close()
        con.close()


def test_a_schema_57_database_only_gains_mcp_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database already at 57 takes only 58's step: the table is added, and nothing else is rewritten (a
    board stored at 57 is byte for byte as it was, and the reviews table's SQL is the one 57 left). Fails if
    the step is missing, starts from another version, or rewrites rows."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    try:
        old.executescript(FIXTURE.read_text())
    finally:
        old.close()
    stored = json.dumps([el("kept", text="a note", originalText="a note")], separators=(",", ":"))
    q = "SELECT sql FROM sqlite_master WHERE name='reviews'"
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 57)
        at57 = db.open_db(path)
        assert db.schema_version(at57) == 57
        with db.tx(at57):
            at57.execute(
                "INSERT INTO room_boards(room_id, seq, elements, updated_at) VALUES(1, 2, ?, 1.0)", (stored,)
            )
        counts = db.row_counts(at57, db.tables_of(57))
        reviews_sql = at57.execute(q).fetchone()[0]
        at57.close()
    con = db.open_db(path)
    backup = db.connect(db.backup_path_for(path, 57), readonly=True)
    try:
        assert db.schema_version(con) == 58
        assert db.schema_version(backup) == 57
        assert db.row_counts(con, db.TABLES) == {**counts, "mcp_sessions": 0}
        same_table_as_fresh(con, tmp_path)
        assert con.execute("SELECT elements FROM room_boards WHERE room_id=1").fetchone()[0] == stored
        assert con.execute(q).fetchone()[0] == reviews_sql
        assert db.integrity_ok(con) is None
    finally:
        backup.close()
        con.close()


def test_a_schema_58_database_opens_unchanged(tmp_path: Path) -> None:
    """A database at the current version is opened as it is: no step, no backup, its rows (an MCP session's
    record included) kept. Fails if opening a 58 database migrates or backs it up again."""
    path = tmp_path / "now.db"
    con = db.open_db(path)
    with db.tx(con):
        con.execute(
            "INSERT INTO agent_tokens(person_id, device, token_hash, created_at)"
            " VALUES(NULL, 'laptop', ?, 1.0)",
            ("a" * 64,),
        )
        con.execute(
            "INSERT INTO mcp_sessions(token_id, sid_hash, client_name, client_version, protocol, opened_at)"
            " VALUES(1, ?, 'Cursor', '1', '2025-06-18', 1.0)",
            ("b" * 64,),
        )
    schema = sorted(tuple(r) for r in con.execute("SELECT type, name, sql FROM sqlite_master"))
    con.close()
    again = db.open_db(path)
    try:
        assert db.schema_version(again) == 58
        assert sorted(tuple(r) for r in again.execute("SELECT type, name, sql FROM sqlite_master")) == schema
        assert [tuple(r) for r in again.execute("SELECT client_name FROM mcp_sessions")] == [("Cursor",)]
        assert not list(tmp_path.glob("now.db.v*.bak*"))
    finally:
        again.close()

"""Schema 57 keeps which client opened each MCP session, so a restart can open it again (DESIGN.md §49.2,
#823). The step is from 56 (#893's whiteboard cleaning, #896), so a schema 55 database goes through that
cleaning first, and a schema 56 one only gains the table."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_db_migrate_v56 import CANARIES, at_55, el

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"
QUERY = "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='mcp_sessions' ORDER BY name"
TOKEN = CANARIES[0]


def same_table_as_fresh(con: sqlite3.Connection, tmp_path: Path) -> None:
    fresh = db.open_db(tmp_path / "fresh.db")
    try:
        got = [tuple(r) for r in con.execute(QUERY)]
        assert got == [tuple(r) for r in fresh.execute(QUERY)]
        assert len(got) >= 2  # the table and its index
    finally:
        fresh.close()


def test_a_schema_55_database_is_cleaned_by_56_and_gains_mcp_sessions_at_57(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real schema 55 database goes through both steps in one upgrade: 56's whiteboard cleaning runs (no
    canary is left in the stored board) and 57 adds an empty table the same as a fresh database's, every
    other row kept. Fails if 57 skips 56 (as the step from 55 to 57 did) or the table differs."""
    path = at_55(tmp_path, monkeypatch)
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 55)
        before = db.open_db(path)
        counts = db.row_counts(before, db.tables_of(55))
        before.close()
    con = db.open_db(path)
    backup = db.connect(db.backup_path_for(path, 55), readonly=True)
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION == 57
        assert db.schema_version(backup) == 55
        assert db.row_counts(con, db.TABLES) == {**counts, "mcp_sessions": 0}
        same_table_as_fresh(con, tmp_path)
        assert not backup.execute(QUERY).fetchall()
        raw = con.execute("SELECT elements FROM room_boards WHERE room_id=1").fetchone()[0]
        assert {e["id"] for e in json.loads(raw)} == {
            "clean1", "wrapped", "ten_equal", "ten_wrapped", "arrow1", "label", "holder",
        }  # fmt: skip
        for canary in CANARIES:
            assert canary not in raw, canary
        assert not con.execute("SELECT 1 FROM meta WHERE key=?", (db.CLEAN_PENDING,)).fetchone()
        assert db.integrity_ok(con) is None
    finally:
        backup.close()
        con.close()


def test_a_schema_56_database_only_gains_mcp_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database already at 56 takes only 57's step: the table is added, and nothing is cleaned again (a
    board stored at 56 is byte for byte as it was, since 56's cleaning is not 57's). Fails if the step is
    missing, starts from another version, or rewrites rows."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    try:
        old.executescript(FIXTURE.read_text())
    finally:
        old.close()
    stored = json.dumps([el("kept", text="a note", originalText="a note")], separators=(",", ":"))
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 56)
        at56 = db.open_db(path)
        assert db.schema_version(at56) == 56
        with db.tx(at56):
            at56.execute(
                "INSERT INTO room_boards(room_id, seq, elements, updated_at) VALUES(1, 2, ?, 1.0)", (stored,)
            )
        counts = db.row_counts(at56, db.tables_of(56))
        at56.close()
    con = db.open_db(path)
    backup = db.connect(db.backup_path_for(path, 56), readonly=True)
    try:
        assert db.schema_version(con) == 57
        assert db.schema_version(backup) == 56
        assert db.row_counts(con, db.TABLES) == {**counts, "mcp_sessions": 0}
        same_table_as_fresh(con, tmp_path)
        assert con.execute("SELECT elements FROM room_boards WHERE room_id=1").fetchone()[0] == stored
        assert db.integrity_ok(con) is None
    finally:
        backup.close()
        con.close()


def test_a_schema_57_database_opens_unchanged(tmp_path: Path) -> None:
    """A database at the current version is opened as it is: no step, no backup, its rows (an MCP session's
    record included) kept. Fails if opening a 57 database migrates or backs it up again."""
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
        assert db.schema_version(again) == 57
        assert sorted(tuple(r) for r in again.execute("SELECT type, name, sql FROM sqlite_master")) == schema
        assert [tuple(r) for r in again.execute("SELECT client_name FROM mcp_sessions")] == [("Cursor",)]
        assert not list(tmp_path.glob("now.db.v*.bak*"))
    finally:
        again.close()

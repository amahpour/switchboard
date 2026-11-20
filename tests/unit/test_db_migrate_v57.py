"""Schema 57 keeps which client opened each MCP session, so a restart can open it again (DESIGN.md §49.2,
#823). The step is from 55: 56 is #896's, not merged yet."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def test_mcp_session_table_matches_fresh_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An upgrade from 55 keeps all its rows and starts with an empty, indexed table the same as a fresh
    database's. Fails if the step is missing or its table differs from the one a new database gets."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    try:
        old.executescript(FIXTURE.read_text())
    finally:
        old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 55)
        before = db.open_db(path)
        counts = db.row_counts(before, db.tables_of(55))
        before.close()
    upgraded = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    backup = db.connect(path.with_name("old.db.v55.bak"), readonly=True)
    try:
        query = "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='mcp_sessions' ORDER BY name"
        assert db.schema_version(upgraded) == db.SCHEMA_VERSION == 57
        assert db.schema_version(backup) == 55
        assert db.row_counts(upgraded, db.TABLES) == {**counts, "mcp_sessions": 0}
        assert [tuple(r) for r in upgraded.execute(query)] == [tuple(r) for r in fresh.execute(query)]
        assert len(upgraded.execute(query).fetchall()) >= 2  # table and its index
        assert not backup.execute(query).fetchall()
        assert db.integrity_ok(upgraded) is None
    finally:
        backup.close()
        upgraded.close()
        fresh.close()

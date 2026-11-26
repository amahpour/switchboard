"""Schema 53 stores say retry receipts (DESIGN.md §8.4, #823). The step is from 52."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

NEW_AT_60 = {"webhooks": 0, "webhook_sent": 0}  # 60's tables (#923), empty with no webhook rows to move

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def test_retry_receipt_table_matches_fresh_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An upgrade from 52 keeps all its rows and starts with an empty, indexed receipt table."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    try:
        old.executescript(FIXTURE.read_text())
    finally:
        old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 52)
        before = db.open_db(path)
        counts = db.row_counts(before, db.tables_of(52))
        before.close()
    upgraded = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    backup = db.connect(path.with_name("old.db.v52.bak"), readonly=True)
    try:
        query = "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='say_keys' ORDER BY name"
        assert db.schema_version(upgraded) == db.SCHEMA_VERSION == 63
        assert db.schema_version(backup) == 52
        assert db.row_counts(upgraded, db.TABLES) == {**counts, "say_keys": 0, "mcp_sessions": 0, **NEW_AT_60}
        assert [tuple(r) for r in upgraded.execute(query)] == [tuple(r) for r in fresh.execute(query)]
        assert len(upgraded.execute(query).fetchall()) >= 2  # table and expiry index
        assert not backup.execute(query).fetchall()
        assert db.integrity_ok(upgraded) is None
    finally:
        backup.close()
        upgraded.close()
        fresh.close()

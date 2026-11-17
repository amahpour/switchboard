"""Schema 51 indexes the usage page's window on a real old database (DESIGN.md §61, #818)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"
INDEXES = ("messages_agent_ts", "batches_wake_created")


def at_50(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 50)
        db.open_db(path).close()


def test_the_usage_indexes_are_added_and_match_a_fresh_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database at schema 50 has neither index. After the step both exist, their SQL matches a
    fresh database, the messages are untouched, and the backup is still schema 50 without them.
    Fails if the indexes are only in the fresh CREATE, or the migration drops rows."""
    path = tmp_path / "old.db"
    at_50(path, monkeypatch)
    with sqlite3.connect(path) as before:
        assert db.schema_version(before) == 50
        for name in INDEXES:
            assert before.execute("SELECT name FROM sqlite_master WHERE name=?", (name,)).fetchone() is None
        total = before.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 51
    assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == total
    fresh = db.open_db(tmp_path / "fresh.db")
    q = (
        "SELECT name, sql FROM sqlite_master WHERE name IN"
        " ('messages_agent_ts', 'batches_wake_created') ORDER BY name"
    )
    assert [tuple(r) for r in con.execute(q)] == [tuple(r) for r in fresh.execute(q)]
    assert len(con.execute(q).fetchall()) == 2
    assert db.integrity_ok(con) is None
    with sqlite3.connect(path.with_name("old.db.v50.bak")) as backup:
        assert db.schema_version(backup) == 50
        for name in INDEXES:
            assert backup.execute("SELECT name FROM sqlite_master WHERE name=?", (name,)).fetchone() is None
    fresh.close()
    con.close()

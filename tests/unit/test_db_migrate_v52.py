"""Schema 52 indexes the usage page's window on a database already at schema 51 (DESIGN.md §61, #818).

Schema 51 (#816) is the review opt-in. A database that stopped there has ``review_on_link`` and neither
usage index. This step adds the indexes and leaves that column and the messages alone.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"
INDEXES = ("messages_agent_ts", "batches_wake_created")


def at_51(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 51)
        db.open_db(path).close()


def test_the_usage_indexes_are_added_and_match_a_fresh_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database at schema 51 has the review column and neither index. After the step both indexes exist,
    their SQL matches a fresh database, the messages and the review column are untouched, and the backup is
    still schema 51 without the indexes. Fails if the indexes are only in the fresh CREATE, if the step
    runs against schema 50 and skips a database #816 already migrated, or if the migration drops rows."""
    path = tmp_path / "old.db"
    at_51(path, monkeypatch)
    with sqlite3.connect(path) as before:
        assert db.schema_version(before) == 51
        assert "review_on_link" in {r[1] for r in before.execute("PRAGMA table_info(rooms)")}
        for name in INDEXES:
            assert before.execute("SELECT name FROM sqlite_master WHERE name=?", (name,)).fetchone() is None
        total = before.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        rooms_off = before.execute("SELECT COUNT(*) FROM rooms WHERE review_on_link=0").fetchone()[0]
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 60
    assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == total
    assert con.execute("SELECT COUNT(*) FROM rooms WHERE review_on_link=0").fetchone()[0] == rooms_off
    fresh = db.open_db(tmp_path / "fresh.db")
    q = (
        "SELECT name, sql FROM sqlite_master WHERE name IN"
        " ('messages_agent_ts', 'batches_wake_created') ORDER BY name"
    )
    assert [tuple(r) for r in con.execute(q)] == [tuple(r) for r in fresh.execute(q)]
    assert len(con.execute(q).fetchall()) == 2
    assert db.integrity_ok(con) is None
    with sqlite3.connect(path.with_name("old.db.v51.bak")) as backup:
        assert db.schema_version(backup) == 51
        assert "review_on_link" in {r[1] for r in backup.execute("PRAGMA table_info(rooms)")}
        for name in INDEXES:
            assert backup.execute("SELECT name FROM sqlite_master WHERE name=?", (name,)).fetchone() is None
    fresh.close()
    con.close()

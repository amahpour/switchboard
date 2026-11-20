"""Schema 51 adds a room's opt-in to a review asked for by a posted link, to a real old database (§37.16)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def test_the_setting_defaults_off_and_old_rows_and_backup_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migrate the release fixture to 50 first, then on through the current schema. The new column's CHECK
    holds, and every old room starts with the setting off. Fails if a room starts on, or a later step drops
    the column."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 50)
        old = db.open_db(path)
        messages = [tuple(r) for r in old.execute("SELECT * FROM messages")]
        rooms = old.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        old.close()
    assert rooms > 0
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 57
    assert [tuple(r) for r in con.execute("SELECT * FROM messages")] == messages
    assert con.execute("SELECT COUNT(*) FROM rooms WHERE review_on_link=0").fetchone()[0] == rooms
    fresh = db.open_db(tmp_path / "fresh.db")
    assert [tuple(r) for r in con.execute("PRAGMA table_info(rooms)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(rooms)")
    ]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE rooms SET review_on_link=2")
    assert db.integrity_ok(con) is None
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    with sqlite3.connect(path.with_name("old.db.v50.bak")) as backup:
        assert db.schema_version(backup) == 50
        assert "review_on_link" not in {r[1] for r in backup.execute("PRAGMA table_info(rooms)")}
    fresh.close()
    con.close()

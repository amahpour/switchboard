"""Schema 48 adds a room's hands-free call settings to a real old database (DESIGN.md §52.3)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def test_both_settings_default_off_and_old_rows_and_backup_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migrate the release fixture to 47 first, then check the new columns, their CHECKs, and that every
    old room starts with calls as they were (Voice off, listening opt-in)."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 47)
        old = db.open_db(path)
        messages = [tuple(r) for r in old.execute("SELECT * FROM messages")]
        rooms = old.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        old.close()
    assert rooms > 0
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 50
    assert [tuple(r) for r in con.execute("SELECT * FROM messages")] == messages
    assert (
        con.execute("SELECT COUNT(*) FROM rooms WHERE call_voice=0 AND call_listen=0").fetchone()[0] == rooms
    )
    fresh = db.open_db(tmp_path / "fresh.db")
    assert [tuple(r) for r in con.execute("PRAGMA table_info(rooms)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(rooms)")
    ]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE rooms SET call_voice=2")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE rooms SET call_listen=-1")
    assert db.integrity_ok(con) is None
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    with sqlite3.connect(path.with_name("old.db.v47.bak")) as backup:
        assert db.schema_version(backup) == 47
        assert "call_voice" not in {r[1] for r in backup.execute("PRAGMA table_info(rooms)")}
    fresh.close()
    con.close()

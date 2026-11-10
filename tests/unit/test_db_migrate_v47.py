"""Schema 47 adds opt-in listening to a real old database (DESIGN.md §52.1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def test_listening_defaults_off_and_old_rows_and_backup_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migrate the release fixture to 46 first, then check the exact new columns and their CHECKs."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 46)
        old = db.open_db(path)
        messages = [tuple(r) for r in old.execute("SELECT * FROM messages")]
        members = old.execute("SELECT COUNT(*) FROM memberships").fetchone()[0]
        old.close()
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 49
    assert [tuple(r) for r in con.execute("SELECT * FROM messages")] == messages
    assert (
        con.execute(
            "SELECT COUNT(*) FROM memberships WHERE listen=0 AND listen_call_id IS NULL AND listen_wakes=0"
        ).fetchone()[0]
        == members
    )
    fresh = db.open_db(tmp_path / "fresh.db")
    assert [tuple(r) for r in con.execute("PRAGMA table_info(memberships)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(memberships)")
    ]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE memberships SET listen=2")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE memberships SET listen_wakes=-1")
    assert db.integrity_ok(con) is None
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    with sqlite3.connect(path.with_name("old.db.v46.bak")) as backup:
        assert db.schema_version(backup) == 46
        assert "listen" not in {r[1] for r in backup.execute("PRAGMA table_info(memberships)")}
    fresh.close()
    con.close()

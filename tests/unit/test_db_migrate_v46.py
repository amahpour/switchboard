"""Schema 46: one whiteboard per room, and a short record of each accepted change (DESIGN.md §56, #656)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def at_45(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Migrate the real old database only as far as 45, so 46 is the step under test."""
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 45)
        con = db.open_db(path)
        con.close()


def test_a_room_gains_an_empty_board_and_the_old_rows_stay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration adds the two tables empty, matching a fresh database, and keeps a schema-45
    backup. Fails if a room's messages move, the new tables are missing, or an old backup is
    counted as already having them."""
    path = tmp_path / "old.db"
    at_45(path, monkeypatch)
    with sqlite3.connect(path) as before:
        messages = before.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert "room_boards" not in {r[0] for r in before.execute("SELECT name FROM sqlite_master")}
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 49
    assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == messages
    assert con.execute("SELECT COUNT(*) FROM room_boards").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM room_board_changes").fetchone()[0] == 0
    fresh = db.open_db(tmp_path / "new.db")
    for table in ("room_boards", "room_board_changes"):
        assert [tuple(r) for r in con.execute(f"PRAGMA table_info({table})")] == [
            tuple(r) for r in fresh.execute(f"PRAGMA table_info({table})")
        ]
    assert db.integrity_ok(con) is None
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    con.close()
    fresh.close()
    with sqlite3.connect(path.with_name("old.db.v45.bak")) as backup:
        assert db.schema_version(backup) == 45
        names = {r[0] for r in backup.execute("SELECT name FROM sqlite_master")}
        assert "room_boards" not in names and "room_board_changes" not in names
    assert "room_boards" not in db.tables_of(45)
    assert "room_boards" in db.tables_of(46) and "room_board_changes" in db.tables_of(46)

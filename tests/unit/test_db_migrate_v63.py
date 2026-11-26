"""Schema 63 adds ``rooms.quiet`` (#975, DESIGN.md §50.1): a room's setting, agents here join quiet. The step
from 62 adds the column at 0 for every room and touches nothing else. It runs on ``v1_14_0.sql``, a real
database 1.14.0 wrote (``tests/fixtures/db/README.md``), brought to schema 63 through every step."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v1_14_0.sql"


def test_every_room_gets_quiet_off_and_no_row_is_lost(tmp_path: Path) -> None:
    """A real old database gains the column at 0 on every room, 0 and 1 are the only values it takes, the
    row counts and the rooms' names and rules are unchanged, and the backup is the 1.14.0 file without it.
    Fails if a room starts quiet, the CHECK is missing, or a row is lost."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with sqlite3.connect(path) as before:
        counts = db.row_counts(before, db.TABLES)
        rooms = [
            tuple(r) for r in before.execute("SELECT id, name, created_by, rules_text FROM rooms ORDER BY id")
        ]
    assert rooms
    con = db.open_db(path)
    backup = db.connect(db.backup_path_for(path, 60), readonly=True)
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION == 64
        assert db.row_counts(con, db.TABLES) == counts
        assert [r[0] for r in con.execute("SELECT quiet FROM rooms")] == [0] * len(rooms)
        assert [
            tuple(r) for r in con.execute("SELECT id, name, created_by, rules_text FROM rooms ORDER BY id")
        ] == rooms
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE rooms SET quiet=2")
        assert "quiet" not in {r[1] for r in backup.execute("PRAGMA table_info(rooms)")}
        assert db.integrity_ok(con) is None
    finally:
        backup.close()
        con.close()

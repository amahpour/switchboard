"""Schema 65 (#976, DESIGN.md §50.2): a room's coordinator. The step from 64 only adds the
``room_coordinators`` table, empty, so no room has a coordinator after an upgrade and every other row is
as it was."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v1_14_0.sql"
TABLE = "SELECT sql FROM sqlite_master WHERE name='room_coordinators'"


def test_a_schema_64_database_gains_an_empty_coordinator_table_and_keeps_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database stored at 64 comes through with its rooms and members as they were and no coordinator; the
    table's text is the fresh schema's; its checks hold (a membership can be the coordinator of one room, one
    coordinator a room, a list over 100 characters is refused) and a row goes with its membership or room.
    The backup is schema 64 and has no such table. Fails if the step is missing, names a coordinator, or words
    the table unlike a fresh database."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 64)
        at64 = db.open_db(path)
        with db.tx(at64):
            at64.execute(
                "INSERT INTO rooms(name, created_at, created_by, budget_per_hour, budget_remaining,"
                " budget_window_start, hop_limit) VALUES('#r', 1.0, 'alice', 0, 0, 1.0, 6)"
            )
        counts = db.row_counts(at64, db.tables_of(64))
        before = [tuple(r) for r in at64.execute("SELECT id, name FROM rooms")]
        at64.close()
    con = db.open_db(path)
    backup = db.connect(db.backup_path_for(path, 64), readonly=True)
    fresh = db.open_db(tmp_path / "fresh.db")
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION == 65
        assert db.schema_version(backup) == 64
        assert backup.execute(TABLE).fetchone() is None
        assert db.row_counts(con, db.tables_of(64)) == counts
        assert con.execute("SELECT COUNT(*) FROM room_coordinators").fetchone()[0] == 0
        assert [tuple(r) for r in con.execute("SELECT id, name FROM rooms")] == before
        assert con.execute(TABLE).fetchone()[0] == fresh.execute(TABLE).fetchone()[0]
        with db.tx(con):
            rid = con.execute("SELECT id FROM rooms").fetchone()[0]
            pid = con.execute(
                "INSERT INTO participants(harness, session_key, status, created_at)"
                " VALUES('test','test:a','idle',0)"
            ).lastrowid
            mid = con.execute(
                "INSERT INTO memberships(room_id, participant_id, screen_name, joined_at, join_msg_id)"
                " VALUES(?,?,'a',0,0)",
                (rid, pid),
            ).lastrowid
            con.execute(
                "INSERT INTO room_coordinators(room_id, membership_id, actions, named_by, named_at)"
                " VALUES(?,?,'quiet','alice',1.0)",
                (rid, mid),
            )
        with pytest.raises(sqlite3.IntegrityError):  # one coordinator a room
            con.execute(
                "INSERT INTO room_coordinators(room_id, membership_id, actions, named_by, named_at)"
                " VALUES(?,?,'','alice',1.0)",
                (rid, mid),
            )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE room_coordinators SET actions=?", ("x" * 101,))
        with db.tx(con):
            con.execute("DELETE FROM memberships WHERE id=?", (mid,))
        assert con.execute("SELECT COUNT(*) FROM room_coordinators").fetchone()[0] == 0
        assert db.integrity_ok(con) is None
    finally:
        fresh.close()
        backup.close()
        con.close()

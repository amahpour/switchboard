"""Schema 64 (#984, DESIGN.md §37.20): a board can hold the fix lines it has not posted yet. The step from 63
only adds ``reviews.held_fixes``, empty on every board already there, and the table reads as a fresh one's."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v1_14_0.sql"
REVIEWS = "SELECT sql FROM sqlite_master WHERE name='reviews'"


def test_a_schema_63_database_gains_an_empty_held_fixes_and_keeps_every_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board stored at 63 comes through with its row as it was and ``held_fixes`` empty; the table's text
    is the fresh schema's; the column refuses a value over its cap. The backup is schema 63 and has no such
    column. Fails if the step is missing, rewrites a row, leaves a board holding something, or words the
    column unlike a fresh database."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 63)
        at63 = db.open_db(path)
        with db.tx(at63):
            at63.execute(
                "INSERT INTO rooms(name, created_at, created_by, budget_per_hour, budget_remaining,"
                " budget_window_start, hop_limit) VALUES('#r', 1.0, 'alice', 0, 0, 1.0, 6)"
            )
            at63.execute(
                "INSERT INTO reviews(room_id, url, opened_by, opened_at)"
                " SELECT id, 'https://example.com/a/pull/1', 'bob', 2.0 FROM rooms WHERE name='#r'"
            )
        counts = db.row_counts(at63, db.tables_of(63))
        before = [tuple(r) for r in at63.execute("SELECT id, url, opened_by, opened_at FROM reviews")]
        at63.close()
    con = db.open_db(path)
    backup = db.connect(db.backup_path_for(path, 63), readonly=True)
    fresh = db.open_db(tmp_path / "fresh.db")
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION == 65
        assert db.schema_version(backup) == 63
        assert "held_fixes" not in backup.execute(REVIEWS).fetchone()[0]
        assert db.row_counts(con, db.tables_of(64)) == counts  # 65 only adds a table (room_coordinators)
        assert [tuple(r) for r in con.execute("SELECT id, url, opened_by, opened_at FROM reviews")] == before
        assert [r[0] for r in con.execute("SELECT held_fixes FROM reviews")] == [""] * len(before)
        assert con.execute(REVIEWS).fetchone()[0] == fresh.execute(REVIEWS).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE reviews SET held_fixes=?", ("x" * 30001,))
        assert db.integrity_ok(con) is None
    finally:
        fresh.close()
        backup.close()
        con.close()

"""Schema 45: what each person has read of each room, and the review boards they set aside, for the home page
(#663, DESIGN.md §55.8).

Two new tables, empty after the step. The database is the real 0.37.2 one, migrated to 44 first, so the step
runs on rows that exist (rooms and messages), and it keeps every one of them. The backup written first is
that schema-44 file, which has neither table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def at_44(path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Migrate the real old database only to 44. Returns its row count in every table."""
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 44)
        con = db.open_db(path)
        assert db.schema_version(con) == 44
        counts = db.row_counts(con, db.tables_of(44))
        con.close()
    return counts


def test_the_two_tables_are_new_and_empty_and_match_a_fresh_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every old row survives, the tables have the same columns as a fresh database's, the marks refuse a
    negative id and a room that is not there, and the backup is the schema-44 file. Fails if the step loses
    a row, the two schemas differ, or a mark can name a room that does not exist."""
    path = tmp_path / "old.db"
    before = at_44(path, monkeypatch)
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 45
    after = db.row_counts(con, db.tables_of(44))
    assert after == before
    fresh = db.open_db(tmp_path / "new.db")
    for table in ("home_seen", "home_snoozes"):
        assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert [tuple(r) for r in con.execute(f"PRAGMA table_info({table})")] == [
            tuple(r) for r in fresh.execute(f"PRAGMA table_info({table})")
        ]
    room = con.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
    con.execute("INSERT INTO home_seen(person_key, room_id, seen_id) VALUES(0, ?, 3)", (room,))
    con.execute(
        "INSERT INTO home_snoozes(person_key, room_id, review_id, snoozed_at) VALUES(0, ?, 1, 2.5)", (room,)
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO home_seen(person_key, room_id, seen_id) VALUES(0, ?, -1)", (room,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO home_seen(person_key, room_id, seen_id) VALUES(-1, ?, 1)", (room,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO home_seen(person_key, room_id, seen_id) VALUES(0, 99999, 1)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO home_snoozes(person_key, room_id, review_id, snoozed_at) VALUES(0, 99999, 1, 1)"
        )
    assert db.integrity_ok(con) is None
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    con.close()
    fresh.close()
    with sqlite3.connect(path.with_name("old.db.v44.bak")) as backup:
        assert db.schema_version(backup) == 44
        names = {r[0] for r in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "home_seen" not in names and "home_snoozes" not in names

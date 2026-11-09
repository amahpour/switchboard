"""Schema 37 adds ``claims.note`` (#545, DESIGN.md §47).

A claim's note is its state or a link. Every claim that already exists gets an empty
note, so a room's claims read as they did. The column is ``TEXT NOT NULL DEFAULT ''``:
there is no NULL to treat as a note. The database is the real 0.37.2 one
(``tests/fixtures/db/v0_37_2.sql``, schema 28), migrated to 36 first, with one claim
planted there, then this step. The backup written first is that schema-36 file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"


def at_36(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = sqlite3.connect(path)
    old.executescript(FIXTURE.read_text())
    old.commit()
    old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 36)
        con = db.open_db(path)
        assert db.schema_version(con) == 36
        assert "note" not in {r[1] for r in con.execute("PRAGMA table_info(claims)")}
        con.close()


def test_claims_gain_an_empty_note_and_the_backup_is_schema_36(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The planted claim survives with note '', NULL is refused, and the backup is the
    schema-36 database without the column. Fails if the step drops the row, leaves the
    column off, or lets a note be NULL."""
    path = tmp_path / "old.db"
    at_36(path, monkeypatch)
    before = sqlite3.connect(path)
    room = before.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
    member = before.execute("SELECT id FROM memberships LIMIT 1").fetchone()[0]
    had = before.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    before.execute(
        "INSERT INTO claims(room_id, key_norm, key, membership_id, expires_at) VALUES(?,?,?,?,?)",
        (room, "build", "build", member, 9_000_000_000.0),
    )
    before.commit()
    counts = db.row_counts(before, db.tables_of(36))
    assert counts["claims"] == had + 1
    before.close()

    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 45
    assert db.row_counts(con, db.tables_of(36)) == counts
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    assert db.integrity_ok(con) is None
    notes = [row[0] for row in con.execute("SELECT note FROM claims")]
    assert notes and set(notes) == {""} and len(notes) == counts["claims"]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE claims SET note=NULL WHERE key='build'")
    con.close()

    backup = sqlite3.connect(path.with_name("old.db.v36.bak"))
    assert db.schema_version(backup) == 36
    assert "note" not in {r[1] for r in backup.execute("PRAGMA table_info(claims)")}
    assert backup.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == counts["claims"]
    backup.close()

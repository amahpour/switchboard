"""Schema 36 adds ``memberships.quiet`` (#548, DESIGN.md §50).

Every membership that already exists stays off (0), so delivery is unchanged until a
person turns it on. The column is NOT NULL and only 0 or 1: there is no unknown mode.
The database is the real 0.37.2 one (``tests/fixtures/db/v0_37_2.sql``, schema 28),
migrated to 35 first, then this step. The backup written first is that schema-35 file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"


def at_35(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = sqlite3.connect(path)
    old.executescript(FIXTURE.read_text())
    old.commit()
    old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 35)
        con = db.open_db(path)
        assert db.schema_version(con) == 35
        assert "quiet" not in {r[1] for r in con.execute("PRAGMA table_info(memberships)")}
        con.close()


def test_memberships_gain_quiet_off_and_the_backup_is_schema_35(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows survive, every membership is not quiet, 2 is refused, and the backup is the
    schema-35 database. Fails if the step drops a row, leaves the column off, or lets
    a value other than 0 or 1 through."""
    path = tmp_path / "old.db"
    at_35(path, monkeypatch)
    before = sqlite3.connect(path)
    counts = db.row_counts(before, db.tables_of(35))
    members = before.execute("SELECT COUNT(*) FROM memberships").fetchone()[0]
    before.close()
    assert members > 0 and counts["memberships"] == members

    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 46
    assert db.row_counts(con, db.tables_of(35)) == counts
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    assert db.integrity_ok(con) is None
    quiet = con.execute("SELECT quiet FROM memberships").fetchall()
    assert quiet and {row[0] for row in quiet} == {0}
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE memberships SET quiet=2 WHERE id=(SELECT MIN(id) FROM memberships)")
    con.close()

    backup = sqlite3.connect(path.with_name("old.db.v35.bak"))
    assert db.schema_version(backup) == 35
    assert db.row_counts(backup, db.tables_of(35)) == counts
    assert "quiet" not in {r[1] for r in backup.execute("PRAGMA table_info(memberships)")}
    backup.close()

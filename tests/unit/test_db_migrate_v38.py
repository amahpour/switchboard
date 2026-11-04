"""Schema 38 adds ``people.admin`` (#569, DESIGN.md §51).

Every person who already exists stays an ordinary person (0). The owner has no
``people`` row, so nothing here makes them an admin: they are one because they
claimed the broker. The column is ``INTEGER NOT NULL DEFAULT 0`` and only 0 or
1, so there is no unknown flag to treat as on. The database is the real 0.37.2
one (``tests/fixtures/db/v0_37_2.sql``, schema 28), migrated to 37 first, with
the person that database already has (bob), then this step. The backup written
first is that schema-37 file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"


def at_37(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = sqlite3.connect(path)
    old.executescript(FIXTURE.read_text())
    old.commit()
    old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 37)
        con = db.open_db(path)
        assert db.schema_version(con) == 37
        assert "admin" not in {r[1] for r in con.execute("PRAGMA table_info(people)")}
        con.close()


def test_people_gain_admin_off_and_the_backup_is_schema_37(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The planted person survives with admin 0, NULL and 2 are refused, and the backup is
    the schema-37 database without the column. Fails if the step drops the row, leaves the
    column off, or lets a value other than 0 or 1 through."""
    path = tmp_path / "old.db"
    at_37(path, monkeypatch)
    before = sqlite3.connect(path)
    counts = db.row_counts(before, db.tables_of(37))
    assert counts["people"] >= 1
    assert before.execute("SELECT name FROM people WHERE name='bob'").fetchone() is not None
    before.close()

    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 40
    assert db.row_counts(con, db.tables_of(37)) == counts
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    assert db.integrity_ok(con) is None
    admin = con.execute("SELECT admin FROM people WHERE name='bob'").fetchone()[0]
    assert admin == 0
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE people SET admin=NULL WHERE name='bob'")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE people SET admin=2 WHERE name='bob'")
    con.close()

    backup = sqlite3.connect(path.with_name("old.db.v37.bak"))
    assert db.schema_version(backup) == 37
    assert "admin" not in {r[1] for r in backup.execute("PRAGMA table_info(people)")}
    assert backup.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1
    backup.close()

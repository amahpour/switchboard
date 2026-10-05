"""The v4 to v5 migration adds each person's preferences without rewriting older rows."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_v4_database_gains_an_empty_preferences_table_and_a_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    for stmt in db.V3_TO_V4:
        con.execute(stmt)
    con.commit()
    assert db.schema_version(con) == 4
    before = db.row_counts(con, db.V3_TABLES)
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 7
    assert db.row_counts(con, db.V3_TABLES) == before
    assert con.execute("SELECT person_id, theme FROM preferences").fetchall() == []
    assert db.integrity_ok(con) is None
    backup = p.with_name(p.name + ".v4.bak")
    assert backup.exists() and os.stat(backup).st_mode & 0o777 == 0o600
    old = sqlite3.connect(backup)
    assert db.schema_version(old) == 4
    assert db.row_counts(old, db.V3_TABLES) == before
    old.close()
    con.close()

"""The v5 to v6 migration keeps saved themes and adds a default text size."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_v5_preferences_gain_text_size_with_a_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    for stmt in (*db.V3_TO_V4, *db.V4_TO_V5):
        con.execute(stmt)
    con.execute("INSERT INTO preferences(person_id, theme) VALUES(0, 'dark')")
    con.commit()
    assert db.schema_version(con) == 5
    before = db.row_counts(con, db.TABLES)
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 7
    assert db.row_counts(con, db.TABLES) == before
    assert con.execute("SELECT theme, text_size FROM preferences WHERE person_id=0").fetchone()[:] == (
        "dark",
        "default",
    )
    assert db.integrity_ok(con) is None
    backup = p.with_name(p.name + ".v5.bak")
    assert backup.exists() and os.stat(backup).st_mode & 0o777 == 0o600
    old = sqlite3.connect(backup)
    assert db.schema_version(old) == 5
    assert db.row_counts(old, db.TABLES) == before
    old.close()
    con.close()

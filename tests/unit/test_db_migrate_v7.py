"""The v6 to v7 migration preserves preferences and rooms while adding room rules."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_v6_database_gains_rules_with_a_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    for stmt in (*db.V3_TO_V4, *db.V4_TO_V5, *db.V5_TO_V6):
        con.execute(stmt)
    con.execute("INSERT INTO preferences(person_id, theme, text_size) VALUES(0, 'dark', 'large')")
    con.commit()
    assert db.schema_version(con) == 6
    before = db.row_counts(con, db.TABLES)
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 8
    assert db.row_counts(con, db.TABLES) == before
    assert con.execute("SELECT theme, text_size, room_rules FROM preferences WHERE person_id=0").fetchone()[
        :
    ] == ("dark", "large", "")
    assert con.execute("SELECT rules_text FROM rooms LIMIT 1").fetchone()[0] == ""
    assert db.integrity_ok(con) is None
    backup = p.with_name(p.name + ".v6.bak")
    assert backup.exists() and os.stat(backup).st_mode & 0o777 == 0o600
    old = sqlite3.connect(backup)
    assert db.schema_version(old) == 6
    assert db.row_counts(old, db.TABLES) == before
    old.close()
    con.close()

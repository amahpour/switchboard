"""Schema 18 (#316) adds room aliases while preserving every schema 17 room."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_rooms_start_with_no_aliases_after_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    for to, statements, _new in db._steps(3):
        if to == 18:
            break
        for stmt in statements:
            con.execute(stmt)
    con.commit()
    assert db.schema_version(con) == 17
    before = db.row_counts(con, db.TABLES)
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 19
    assert db.row_counts(con, db.TABLES) == before
    assert con.execute("SELECT COUNT(*) FROM rooms WHERE renamed_from <> '[]'").fetchone()[0] == 0
    assert p.with_name(p.name + ".v17.bak").exists()
    con.close()

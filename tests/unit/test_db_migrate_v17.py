"""The v16 to v17 migration (#292, DESIGN.md §6.1): participants gain ``status_line`` and
``status_line_at``, both NULL (no status set) for every session that exists; every row stays,
after a checked backup."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_participants_gain_an_empty_status_line_with_a_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    steps = (*db.V3_TO_V4, *db.V4_TO_V5, *db.V5_TO_V6, *db.V6_TO_V7, *db.V7_TO_V8, *db.V8_TO_V9)
    later = (*db.V9_TO_V10, *db.V10_TO_V11, *db.V11_TO_V12, *db.V12_TO_V13, *db.V13_TO_V14, *db.V14_TO_V15)
    for stmt in (*steps, *later, *db.V15_TO_V16):
        con.execute(stmt)
    con.commit()
    assert db.schema_version(con) == 16
    people = con.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
    assert people > 0  # the fixture has participants, so "all NULL" means something
    before = db.row_counts(con, db.V20_TABLES)
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 24
    assert db.row_counts(con, db.V20_TABLES) == before
    unset = "SELECT COUNT(*) FROM participants WHERE status_line IS NULL AND status_line_at IS NULL"
    assert con.execute(unset).fetchone()[0] == people
    assert p.with_name(p.name + ".v16.bak").exists()
    con.close()

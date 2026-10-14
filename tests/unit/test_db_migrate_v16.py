"""The v15 to v16 migration (#304, DESIGN.md §6.1): memberships gain ``join_code_hash``, NULL for
every membership made before, so none of them can be reclaimed with a code it was never given;
every row stays, after a checked backup."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_memberships_gain_a_null_join_code_with_a_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    steps = (*db.V3_TO_V4, *db.V4_TO_V5, *db.V5_TO_V6, *db.V6_TO_V7, *db.V7_TO_V8, *db.V8_TO_V9)
    later = (*db.V9_TO_V10, *db.V10_TO_V11, *db.V11_TO_V12, *db.V12_TO_V13, *db.V13_TO_V14, *db.V14_TO_V15)
    for stmt in (*steps, *later):
        con.execute(stmt)
    con.commit()
    assert db.schema_version(con) == 15
    members = con.execute("SELECT COUNT(*) FROM memberships").fetchone()[0]
    assert members > 0  # the fixture has memberships, so "all NULL" means something
    before = db.row_counts(con, db.V20_TABLES)
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 24
    assert db.row_counts(con, db.V20_TABLES) == before
    assert (
        con.execute("SELECT COUNT(*) FROM memberships WHERE join_code_hash IS NULL").fetchone()[0] == members
    )
    assert p.with_name(p.name + ".v15.bak").exists()
    con.close()

"""The v12 to v13 migration (#192, DESIGN.md §39.6): people gain a first and a last name, both
empty until set; every row stays, after a checked backup."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_people_gain_empty_first_and_last_names_with_a_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    steps = (*db.V3_TO_V4, *db.V4_TO_V5, *db.V5_TO_V6, *db.V6_TO_V7, *db.V7_TO_V8, *db.V8_TO_V9)
    for stmt in (*steps, *db.V9_TO_V10, *db.V10_TO_V11, *db.V11_TO_V12):
        con.execute(stmt)
    con.execute(
        "INSERT INTO people(name, handle, created_at, email) VALUES('bob', x'01', 1.0, 'bob@example.com')"
    )
    con.commit()
    assert db.schema_version(con) == 12
    before = db.row_counts(con, db.TABLES)
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 13
    assert db.row_counts(con, db.TABLES) == before
    row = con.execute("SELECT name, email, first_name, last_name FROM people").fetchone()
    assert tuple(row) == ("bob", "bob@example.com", None, None)
    assert p.with_name(p.name + ".v12.bak").exists()
    con.close()

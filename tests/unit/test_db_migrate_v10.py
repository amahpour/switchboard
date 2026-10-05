"""The v9 to v10 migration (#70, DESIGN.md §38.3), on to the current schema: every person keeps
their row and starts with no email (#192 renamed v10's Google email to it); an email then
belongs to one active person only."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_people_gain_an_empty_email_with_a_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    for stmt in (*db.V3_TO_V4, *db.V4_TO_V5, *db.V5_TO_V6, *db.V6_TO_V7, *db.V7_TO_V8, *db.V8_TO_V9):
        con.execute(stmt)
    con.execute("INSERT INTO people(name, handle, created_at) VALUES('bob', x'01', 1.0)")
    con.execute("INSERT INTO people(name, handle, created_at) VALUES('carol', x'02', 1.0)")
    con.commit()
    assert db.schema_version(con) == 9
    before = db.row_counts(con, db.TABLES)
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 12
    assert db.row_counts(con, db.TABLES) == before
    assert {r[0] for r in con.execute("SELECT email FROM people")} == {None}
    assert p.with_name(p.name + ".v9.bak").exists()
    con.execute("UPDATE people SET email='bob@example.com' WHERE name='bob'")
    with pytest.raises(sqlite3.IntegrityError):  # one active person per Google email
        con.execute("UPDATE people SET email='bob@example.com' WHERE name='carol'")
    con.close()

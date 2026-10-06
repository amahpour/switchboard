"""The v11 to v12 migration (#192, DESIGN.md §39): a person's email is who they are, and the
Google email (#70) becomes it, for people and for the owner, so nobody signing in with Google
loses their way in."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_google_emails_become_the_accounts_emails_with_a_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    steps = (*db.V3_TO_V4, *db.V4_TO_V5, *db.V5_TO_V6, *db.V6_TO_V7, *db.V7_TO_V8, *db.V8_TO_V9)
    for stmt in (*steps, *db.V9_TO_V10, *db.V10_TO_V11):
        con.execute(stmt)
    con.execute(
        "INSERT INTO people(name, handle, created_at, google_email)"
        " VALUES('bob', x'01', 1.0, 'bob@example.com')"
    )
    con.execute("INSERT INTO people(name, handle, created_at) VALUES('carol', x'02', 1.0)")
    con.execute("INSERT INTO meta(key, value) VALUES('owner_google_email', 'alice@example.com')")
    con.commit()
    assert db.schema_version(con) == 11
    before = db.row_counts(con, db.TABLES)
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 13
    assert db.row_counts(con, db.TABLES) == before
    assert dict(con.execute("SELECT name, email FROM people").fetchall()) == {
        "bob": "bob@example.com",
        "carol": None,
    }
    assert con.execute("SELECT value FROM meta WHERE key='owner_email'").fetchone()[0] == "alice@example.com"
    assert con.execute("SELECT COUNT(*) FROM meta WHERE key='owner_google_email'").fetchone()[0] == 0
    assert p.with_name(p.name + ".v11.bak").exists()
    con.close()

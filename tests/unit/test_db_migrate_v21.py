"""Schema 21 (#293) adds advisory claims without disturbing existing rooms or members."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_v20_upgrade_adds_empty_claims_with_a_backup(tmp_path: Path) -> None:
    path = tmp_path / "switchboard.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    for to, statements, _new in db._steps(3):
        if to == 21:
            break
        for statement in statements:
            con.execute(statement)
    assert db.schema_version(con) == 20
    before = db.row_counts(con, db.TABLES[:-1])
    con.commit()
    con.close()
    os.chmod(path, 0o600)

    migrated = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    assert db.schema_version(migrated) == db.SCHEMA_VERSION == 21
    assert db.row_counts(migrated, db.TABLES[:-1]) == before
    assert migrated.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    assert [tuple(r) for r in migrated.execute("PRAGMA table_info(claims)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(claims)")
    ]
    assert path.with_name(path.name + ".v20.bak").exists()
    migrated.close()
    fresh.close()

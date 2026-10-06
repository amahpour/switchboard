"""The v8 to v9 migration (#80, DESIGN.md §37.3) adds the review boards' two tables and keeps
every row, after a checked backup."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_v8_database_gains_review_boards_with_a_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    for stmt in (*db.V3_TO_V4, *db.V4_TO_V5, *db.V5_TO_V6, *db.V6_TO_V7, *db.V7_TO_V8):
        con.execute(stmt)
    con.execute("UPDATE rooms SET rules_text='be brief'")
    con.commit()
    assert db.schema_version(con) == 8
    before = db.row_counts(con, db.V8_TABLES)
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 14
    assert db.row_counts(con, db.V8_TABLES) == before
    assert db.row_counts(con, ("reviews", "review_items")) == {"reviews": 0, "review_items": 0}
    assert con.execute("SELECT rules_text FROM rooms LIMIT 1").fetchone()[0] == "be brief"
    assert db.integrity_ok(con) is None
    backup = p.with_name(p.name + ".v8.bak")
    assert backup.exists() and os.stat(backup).st_mode & 0o777 == 0o600
    old = sqlite3.connect(backup)
    assert db.schema_version(old) == 8
    assert db.row_counts(old, db.V8_TABLES) == before
    old.close()
    con.close()


def test_a_fresh_database_and_a_migrated_one_have_the_same_review_tables(tmp_path: Path) -> None:
    """The fresh schema and V8_TO_V9 must not drift: same columns, same CHECKs, same index."""
    fresh = db.open_db(tmp_path / "fresh.db")
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    for stmt in (*db.V3_TO_V4, *db.V4_TO_V5, *db.V5_TO_V6, *db.V6_TO_V7, *db.V7_TO_V8):
        con.execute(stmt)
    con.commit()
    con.close()
    os.chmod(p, 0o600)
    migrated = db.open_db(p)

    def shape(c: sqlite3.Connection) -> list[tuple[str, str]]:
        rows = c.execute(
            "SELECT name, sql FROM sqlite_master WHERE tbl_name IN ('reviews', 'review_items') ORDER BY name"
        ).fetchall()
        return [(n, " ".join((s or "").split())) for n, s in rows]

    assert shape(fresh) == shape(migrated) and len(shape(fresh)) >= 3

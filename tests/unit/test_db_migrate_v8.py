"""Schema v8 sends existing room rules once to each existing member (DESIGN.md §35)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_v7_members_start_behind_their_room_rules(tmp_path: Path) -> None:
    """Migration makes current rules new to old members without changing the rows."""
    path = tmp_path / "switchboard.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    for stmt in (*db.V3_TO_V4, *db.V4_TO_V5, *db.V5_TO_V6, *db.V6_TO_V7):
        con.execute(stmt)
    con.execute("UPDATE rooms SET rules_text='Existing guidance.' WHERE id=1")
    con.commit()
    assert db.schema_version(con) == 7
    before = db.row_counts(con, db.V8_TABLES)
    con.close()

    migrated = db.open_db(path)
    assert db.schema_version(migrated) == db.SCHEMA_VERSION == 10
    assert db.row_counts(migrated, db.V8_TABLES) == before
    assert migrated.execute("SELECT rules_version FROM rooms WHERE id=1").fetchone()[0] == 1
    assert {row[0] for row in migrated.execute("SELECT rules_seen FROM memberships")} == {0}
    assert db.integrity_ok(migrated) is None
    migrated.close()

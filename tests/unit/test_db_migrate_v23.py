"""Schema 23 (#371) keeps every review finding and starts each one deferred to nothing."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_v22_review_findings_gain_an_empty_deferred_to_after_a_backup(tmp_path: Path) -> None:
    """An upgrade keeps a dropped finding a plain drop (no ticket), adds the same checked column a
    fresh database has, and backs the old file up first. Fails if the step is missing, the column
    differs from the fresh schema's, or an existing drop comes out deferred."""
    path = tmp_path / "switchboard.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    for to, statements, _new in db._steps(3):
        if to == 23:
            break
        for statement in statements:
            con.execute(statement)
    assert db.schema_version(con) == 22
    room_id = con.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
    con.execute(
        "INSERT INTO reviews(room_id,url,head,opened_by,opened_at) VALUES(?,?,?,?,?)",
        (room_id, "https://example.com/shop/pull/7", "aaa1111", "codex-1", 1.0),
    )
    review_id = con.execute("SELECT id FROM reviews ORDER BY id DESC LIMIT 1").fetchone()[0]
    con.execute(
        "INSERT INTO review_items(review_id,kind,n,state,title,raised_by,reason,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (review_id, "finding", 1, "dropped", "old finding", "codex-1", "not a bug", 2.0, 2.0),
    )
    con.commit()
    before = db.row_counts(con, db.tables_of(22))
    con.close()
    os.chmod(path, 0o600)

    migrated = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    assert db.schema_version(migrated) == db.SCHEMA_VERSION == 23
    assert db.row_counts(migrated, db.tables_of(22)) == before
    row = migrated.execute("SELECT state, reason, deferred_to FROM review_items").fetchone()
    assert tuple(row) == ("dropped", "not a bug", "")
    assert [tuple(r) for r in migrated.execute("PRAGMA table_info(review_items)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(review_items)")
    ]
    assert path.with_name(path.name + ".v22.bak").exists()
    migrated.close()
    fresh.close()

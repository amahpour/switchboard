"""Proof and confirmation on review findings (schema 19, #251)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_old_fixed_findings_stay_settled_and_new_fixes_wait_for_confirmation(tmp_path: Path) -> None:
    """A checked v18 backup keeps rows; legacy fixes remain settled without invented proof."""
    path = tmp_path / "switchboard.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    for frm in range(3, 18):
        statements = db._steps(frm)[0][1]
        for statement in statements:
            con.execute(statement)
    assert db.schema_version(con) == 18
    room_id = con.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
    con.execute(
        "INSERT INTO reviews(room_id,url,opened_by,opened_at) VALUES(?,?,?,?)",
        (room_id, "https://example.com/pull/1", "alice", 1.0),
    )
    review_id = con.execute("SELECT id FROM reviews ORDER BY id DESC LIMIT 1").fetchone()[0]
    con.execute(
        "INSERT INTO review_items(review_id,kind,n,state,title,raised_by,owner,commit_sha,"
        "created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (review_id, "finding", 1, "fixed", "old fix", "codex-1", "claude-1", "abc1234", 2.0, 3.0),
    )
    con.commit()
    old_rows = db.row_counts(con, db.TABLES[:-1])
    con.close()
    os.chmod(path, 0o600)

    migrated = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    assert db.schema_version(migrated) == db.SCHEMA_VERSION == 21
    assert db.row_counts(migrated, db.TABLES[:-1]) == old_rows
    assert tuple(migrated.execute("SELECT proof, confirmed_by FROM review_items").fetchone()) == (
        "",
        "legacy",
    )
    assert [tuple(r) for r in migrated.execute("PRAGMA table_info(review_items)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(review_items)")
    ]
    assert path.with_name(path.name + ".v18.bak").exists()
    migrated.close()
    fresh.close()

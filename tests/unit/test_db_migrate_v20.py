"""Schema 20 (#291) keeps existing review findings and starts them without an outdated mark."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_v19_review_findings_gain_an_empty_outdated_head_after_backup(tmp_path: Path) -> None:
    """An upgrade preserves the board and adds the same checked column as a fresh database."""
    path = tmp_path / "switchboard.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    for to, statements, _new in db._steps(3):
        if to == 20:
            break
        for statement in statements:
            con.execute(statement)
    assert db.schema_version(con) == 19
    room_id = con.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
    con.execute(
        "INSERT INTO reviews(room_id,url,head,opened_by,opened_at) VALUES(?,?,?,?,?)",
        (room_id, "https://example.com/shop/pull/7", "aaa1111", "codex-1", 1.0),
    )
    review_id = con.execute("SELECT id FROM reviews ORDER BY id DESC LIMIT 1").fetchone()[0]
    con.execute(
        "INSERT INTO review_items(review_id,kind,n,state,title,raised_by,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (review_id, "finding", 1, "raised", "old finding", "codex-1", 2.0, 2.0),
    )
    con.commit()
    before = db.row_counts(con, db.V20_TABLES)
    con.close()
    os.chmod(path, 0o600)

    migrated = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    assert db.schema_version(migrated) == db.SCHEMA_VERSION == 23
    assert db.row_counts(migrated, db.V20_TABLES) == before
    assert tuple(migrated.execute("SELECT title, outdated_head FROM review_items").fetchone()) == (
        "old finding",
        "",
    )
    assert [tuple(r) for r in migrated.execute("PRAGMA table_info(review_items)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(review_items)")
    ]
    assert path.with_name(path.name + ".v19.bak").exists()
    migrated.close()
    fresh.close()

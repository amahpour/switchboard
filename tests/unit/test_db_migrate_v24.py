"""Schema 24 (#291) keeps every board and starts each one with no report from an agent."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_v23_boards_gain_an_empty_report_after_a_backup(tmp_path: Path) -> None:
    """An upgrade keeps the board, adds the same checked columns a fresh database has, starts it
    with nothing reported, and backs the old file up first. Fails if the step is missing or a
    column differs from the fresh schema's."""
    path = tmp_path / "switchboard.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    for to, statements, _new in db._steps(3):
        if to == 24:
            break
        for statement in statements:
            con.execute(statement)
    assert db.schema_version(con) == 23
    room_id = con.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
    con.execute(
        "INSERT INTO reviews(room_id,url,head,opened_by,opened_at) VALUES(?,?,?,?,?)",
        (room_id, "https://example.com/shop/pull/7", "aaa1111", "codex-1", 1.0),
    )
    con.commit()
    before = db.row_counts(con, db.tables_of(23))
    con.close()
    os.chmod(path, 0o600)

    migrated = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    assert db.schema_version(migrated) == db.SCHEMA_VERSION == 24
    assert db.row_counts(migrated, db.tables_of(23)) == before
    row = migrated.execute(
        "SELECT pr_state, pr_threads, pr_ci, reported_by, reported_at FROM reviews"
    ).fetchone()
    assert tuple(row) == ("", None, "", "", None)
    assert [tuple(r) for r in migrated.execute("PRAGMA table_info(reviews)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(reviews)")
    ]
    assert path.with_name(path.name + ".v23.bak").exists()
    migrated.close()
    fresh.close()

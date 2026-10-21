"""Schema 27 (#424) adds the raiser's reason for insisting on a contested finding, and keeps every row."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db, reviews
from switchboard.store import Store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def database_at_26(path: Path) -> sqlite3.Connection:
    """A database from the 0.7.0 fixture stepped up to schema 26, with a board holding one finding
    contested before agents could insist, one raised and one conceded."""
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    for to, statements, _new in db._steps(3):
        if to == 27:
            break
        for statement in statements:
            con.execute(statement)
    assert db.schema_version(con) == 26
    room_id = con.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
    con.execute(
        "INSERT INTO reviews(room_id,url,head,opened_by,opened_at) VALUES(?,?,?,?,?)",
        (room_id, "https://example.com/shop/pull/7", "aaa1111", "codex-1", 1.0),
    )
    for n, state in enumerate(("contested", "raised", "conceded", "dropped"), 1):
        con.execute(
            "INSERT INTO review_items(review_id,kind,n,state,title,raised_by,reason,created_at,updated_at)"
            " VALUES(1,'finding',?,?,?,'codex-1',?,2.0,2.0)",
            (n, state, f"finding {n}", "half-up is what the receipt shows" if state == "contested" else ""),
        )
    con.commit()
    return con


def test_a_v26_board_keeps_its_rows_and_a_contested_finding_keeps_waiting_on_a_person(tmp_path: Path) -> None:
    """An upgrade keeps every row, adds ``insisted`` as a fresh database has it, and backs the old file
    up first. A finding contested before agents could insist was already waiting on a person and its
    raiser was never asked, so it stays a person's: it is marked insisted with the migration's own
    line, and only that one. Fails if the step is missing, the column differs from a fresh database's,
    or the upgrade quietly hands in-flight contests back to raisers nobody woke."""
    path = tmp_path / "switchboard.db"
    con = database_at_26(path)
    before = db.row_counts(con, db.kept_tables(26))
    con.close()
    os.chmod(path, 0o600)

    migrated = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    assert db.schema_version(migrated) == db.SCHEMA_VERSION == 30
    assert db.row_counts(migrated, db.kept_tables(26)) == before
    assert [tuple(r) for r in migrated.execute("PRAGMA table_info(review_items)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(review_items)")
    ]
    assert path.with_name(path.name + ".v26.bak").exists()
    rows = migrated.execute("SELECT state, insisted FROM review_items ORDER BY n").fetchall()
    assert [tuple(r) for r in rows] == [
        ("contested", reviews.LEGACY_INSISTED), ("raised", ""), ("conceded", ""), ("dropped", ""),
    ]  # fmt: skip
    migrated.close()
    fresh.close()

    store = Store(db.open_db(path), FakeClock())
    contested = next(i for i in store.review_items(1) if i.state == "contested")
    assert reviews.needs_person(contested, {"codex-1", "claude-1"})  # still the person's
    assert reviews.insist_line(contested) == reviews.LEGACY_INSISTED  # and the card says why, plainly


def test_the_insisted_reason_is_bounded_and_the_store_keeps_it(tmp_path: Path) -> None:
    """The column's CHECK is the last line (500 characters, as a contest's reason), and an insist
    changes the finding's reason, not its state. Fails if the column is unbounded, or the store drops it."""
    store = Store(db.open_db(tmp_path / "s.db"), FakeClock())
    room = store.create_room("#build", "alice", 60, 30)
    board = store.open_review(room.id, "https://example.com/shop/pull/7", "", "codex-1")
    item = store.add_review_item(board["id"], "finding", "contested", title="t", raised_by="codex-1")
    store.move_review_item(item.id, "contested", insisted="it stays")
    got = store.review_item(board["id"], "finding", 1)
    assert got is not None and (got.state, got.insisted) == ("contested", "it stays")
    with pytest.raises(sqlite3.IntegrityError):
        store.move_review_item(item.id, "contested", insisted="x" * 501)

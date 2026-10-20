"""Schema 26 (#423) adds the picks agents make among a question's options, and keeps every row."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db, reviews
from switchboard.store import Store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def database_at_25(path: Path) -> sqlite3.Connection:
    """A database from the 0.7.0 fixture stepped up to schema 25, with a board that has a question
    asked with a recommendation (the only pick that existed before: the asker's)."""
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    for to, statements, _new in db._steps(3):
        if to == 26:
            break
        for statement in statements:
            con.execute(statement)
    assert db.schema_version(con) == 25
    room_id = con.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
    con.execute(
        "INSERT INTO reviews(room_id,url,head,opened_by,opened_at) VALUES(?,?,?,?,?)",
        (room_id, "https://example.com/shop/pull/7", "aaa1111", "codex-1", 1.0),
    )
    con.execute(
        "INSERT INTO review_items(review_id,kind,n,state,title,raised_by,options,recommend,created_at,"
        "updated_at) VALUES(1,'question',1,'open','before or after tax?','codex-1',"
        """'["before tax","after tax"]',1,2.0,2.0)"""
    )
    con.commit()
    return con


def test_a_v25_board_keeps_its_question_and_gains_an_empty_picks_table_after_a_backup(tmp_path: Path) -> None:
    """An upgrade keeps every row, adds ``review_picks`` as a fresh database has it (empty, the same
    columns), and backs the old file up first. The asker's recommend stays on the item and is still
    their pick: nothing is rewritten. Fails if the step is missing, the table differs from the fresh
    schema's, or the asker's pick stops counting once the column it lived in is no longer all there is."""
    path = tmp_path / "switchboard.db"
    con = database_at_25(path)
    before = db.row_counts(con, db.tables_of(25))
    assert "review_picks" not in db.tables_of(25)
    con.close()
    os.chmod(path, 0o600)

    migrated = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    assert db.schema_version(migrated) == db.SCHEMA_VERSION == 26
    assert db.row_counts(migrated, db.tables_of(25)) == before
    assert db.tables_of(26)[-1] == "review_picks"
    assert migrated.execute("SELECT COUNT(*) FROM review_picks").fetchone()[0] == 0
    assert [tuple(r) for r in migrated.execute("PRAGMA table_info(review_picks)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(review_picks)")
    ]
    assert path.with_name(path.name + ".v25.bak").exists()
    migrated.close()
    fresh.close()

    store = Store(db.open_db(path), FakeClock())
    [item] = store.review_items(1)
    assert item.picks == () and reviews.effective_picks(item) == {"codex-1": 1}
    c = reviews.consensus(item, ("codex-1", "claude-1"))
    assert (c.state, c.option, c.waiting) == ("proposed", 1, ("claude-1",))


def test_a_pick_is_one_per_agent_and_item_and_within_the_options(tmp_path: Path) -> None:
    """The table's checks are the last line: a second row for one agent and item is refused (the
    store replaces the first instead), an index is 0 to 3, a name 1 to 100 characters, and the item
    must exist. Fails if any of them is missing from the fresh schema."""
    store = Store(db.open_db(tmp_path / "s.db"), FakeClock())
    room = store.create_room("#build", "alice", 60, 30)
    board = store.open_review(room.id, "https://example.com/shop/pull/7", "", "codex-1")
    item = store.add_review_item(board["id"], "question", "open", title="t", raised_by="codex-1")
    con = store.con
    insert = "INSERT INTO review_picks(item_id, agent, choice, picked_at) VALUES(?,?,?,?)"
    con.execute(insert, (item.id, "claude-1", 0, 1.0))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(insert, (item.id, "claude-1", 1, 2.0))
    # the last option a board allows fits; one more doesn't (the CHECK moves with reviews.MAX_OPTIONS)
    con.execute(insert, (item.id, "codex-1", reviews.MAX_OPTIONS - 1, 1.0))
    for bad in (
        ("devin-1", reviews.MAX_OPTIONS, 1.0),
        ("", 0, 1.0),
        ("x" * 101, 0, 1.0),
        ("devin-1", -1, 1.0),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(insert, (item.id, *bad))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(insert, (item.id + 99, "codex-1", 0, 1.0))  # no such item


def test_the_store_keeps_one_pick_per_agent_oldest_first(tmp_path: Path) -> None:
    """``set_review_pick`` replaces an agent's earlier pick (and its time), the picks come back in
    the order they were made, and the item's own state and age are untouched: a pick decides
    nothing. Fails if a second pick adds a row, or moves the item's ``updated_at`` (the 24-hour fix
    deadline reads it)."""
    clock = FakeClock()
    store = Store(db.open_db(tmp_path / "s.db"), clock)
    room = store.create_room("#build", "alice", 60, 30)
    board = store.open_review(room.id, "https://example.com/shop/pull/7", "", "codex-1")
    item = store.add_review_item(
        board["id"], "question", "open", title="t", raised_by="codex-1", options=("a", "b"), recommend=0
    )
    clock.advance(5)
    store.set_review_pick(item.id, "claude-1", 1)
    clock.advance(5)
    store.set_review_pick(item.id, "devin-1", 0)
    clock.advance(5)
    store.set_review_pick(item.id, "claude-1", 0)  # claude-1 changes its mind: now the newest
    got = store.review_item(board["id"], "question", 1)
    assert got is not None
    assert got.picks == (("devin-1", 0), ("claude-1", 0))
    assert (got.state, got.updated_at) == (item.state, item.updated_at)
    assert reviews.effective_picks(got) == {"codex-1": 0, "devin-1": 0, "claude-1": 0}

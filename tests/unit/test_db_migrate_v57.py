"""Schema 57 (#907, DESIGN.md §37.11) lets a review board's reported CI be ``blocked``.

A pull request that conflicts with its base runs no CI. Only the ``pr_ci`` CHECK gains the value, by the
schema-text edit of v33 -> v34, so no board, item or foreign key moves. The boards a schema 56 database
holds are seeded at 56, so a step that dropped or rewrote them would be seen.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"
INSERT = "INSERT INTO reviews(room_id, url, opened_by, opened_at, pr_ci) VALUES (1, ?, 'alice', 1.0, ?)"
BOARD = (
    "SELECT id, room_id, url, head, opened_by, pr_state, pr_threads, pr_ci, reported_by FROM reviews"
    " WHERE id = 7"
)
ITEMS = (
    "SELECT id, review_id, kind, n, state, title, raised_by, options FROM review_items"
    " WHERE review_id = 7 ORDER BY id"
)
PICKS = "SELECT item_id, agent, choice, picked_at FROM review_picks WHERE item_id IN (11, 12) ORDER BY item_id"
SEEDED_BOARDS = [(7, 1, "https://example.com/a/pull/9", "abc123", "alice", "open", 2, "none", "bob")]
SEEDED_ITEMS = [
    (11, 7, "finding", 1, "raised", "a leak", "bob", "[]"),
    (12, 7, "question", 1, "open", "which way", "bob", '["a", "b"]'),
]
SEEDED_PICKS = [(12, "carol", 1, 3.0)]


def at_56(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture (which holds a board of its own) upgraded to schema 56, with another board whose CI is a legacy value, a finding, a question
    and a pick on it: the rows the step must keep."""
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 56)
        db.open_db(path).close()
    with sqlite3.connect(path) as seed:
        seed.execute(
            "INSERT INTO reviews(id, room_id, url, head, opened_by, opened_at, pr_state, pr_threads, pr_ci,"
            " reported_by, reported_at) VALUES (7, 1, 'https://example.com/a/pull/9', 'abc123', 'alice', 1.0,"
            " 'open', 2, 'none', 'bob', 2.0)"
        )
        seed.execute(
            "INSERT INTO review_items(id, review_id, kind, n, state, title, raised_by, created_at, updated_at)"
            " VALUES (11, 7, 'finding', 1, 'raised', 'a leak', 'bob', 1.0, 1.0)"
        )
        seed.execute(
            "INSERT INTO review_items(id, review_id, kind, n, state, title, raised_by, options, created_at,"
            " updated_at) VALUES (12, 7, 'question', 1, 'open', 'which way', 'bob', '[\"a\", \"b\"]', 1.0, 1.0)"
        )
        seed.execute("INSERT INTO review_picks(item_id, agent, choice, picked_at) VALUES (12, 'carol', 1, 3.0)")


def assert_seeded(con: sqlite3.Connection) -> None:
    """The seeded board, its items and the pick, by id and value, and each still points at its parent."""
    assert [tuple(r) for r in con.execute(BOARD)] == SEEDED_BOARDS
    assert [tuple(r) for r in con.execute(ITEMS)] == SEEDED_ITEMS
    assert [tuple(r) for r in con.execute(PICKS)] == SEEDED_PICKS
    joined = "SELECT p.agent, i.kind, r.url FROM review_picks p JOIN review_items i ON i.id = p.item_id JOIN reviews r ON r.id = i.review_id WHERE r.id = 7"  # noqa: E501
    assert [tuple(r) for r in con.execute(joined)] == [("carol", "question", "https://example.com/a/pull/9")]


def test_pr_ci_admits_blocked_and_keeps_every_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A schema 56 database refuses ci "blocked". After the step the seeded board (ci "none"), its finding,
    question and pick are all there with their ids, values and links, the reviews table's SQL matches a
    fresh database's, "blocked" stores, an unknown value is still refused by the CHECK, and the backup is
    still schema 56, holds the same rows and refuses "blocked". Fails if the step is missing, drops or
    rewrites a row, the edit matches nothing, or the CHECK is dropped instead of widened."""
    path = tmp_path / "old.db"
    at_56(path, monkeypatch)
    with sqlite3.connect(path) as before:
        assert db.schema_version(before) == 56
        with pytest.raises(sqlite3.IntegrityError):
            before.execute(INSERT, ("https://example.com/a/pull/1", "blocked"))
        assert_seeded(before)
        counts = db.row_counts(before, db.kept_tables(56))
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 57
    assert db.row_counts(con, db.kept_tables(56)) == counts
    assert_seeded(con)
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
    fresh = db.open_db(tmp_path / "fresh.db")
    q = "SELECT sql FROM sqlite_master WHERE name='reviews'"
    assert con.execute(q).fetchone()[0] == fresh.execute(q).fetchone()[0]
    con.execute(INSERT, ("https://example.com/a/pull/1", "blocked"))
    for bad in ("conflicting", "Blocked"):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(INSERT, ("https://example.com/a/pull/2", bad))
    assert db.integrity_ok(con) is None
    with sqlite3.connect(path.with_name("old.db.v56.bak")) as backup:
        assert db.schema_version(backup) == 56
        assert_seeded(backup)
        with pytest.raises(sqlite3.IntegrityError):
            backup.execute(INSERT, ("https://example.com/a/pull/3", "blocked"))
    fresh.close()
    con.close()


def test_an_unmatched_check_fails_closed_at_56_with_its_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v56 CHECK spelled differently (equally strict, so the needle can't match) must not let the step
    skip the edit: the upgrade refuses, and the database and its backup stay schema 56 with the CHECK text,
    the board and its items as they were. Fails if the step's check is skipped (the database would be
    marked 57 with a CHECK that still refuses "blocked")."""
    path = tmp_path / "old.db"
    at_56(path, monkeypatch)
    with sqlite3.connect(path) as edit:
        edit.execute("PRAGMA writable_schema=ON")
        edit.execute(
            "UPDATE sqlite_master SET sql=replace(sql, '''running'', ''none'')', '''running'',''none'')')"
            " WHERE type='table' AND name='reviews'"
        )
        edit.execute("PRAGMA writable_schema=RESET")
    q = "SELECT sql FROM sqlite_master WHERE name='reviews'"
    with sqlite3.connect(path) as before:
        before_sql = before.execute(q).fetchone()[0]
        counts = db.row_counts(before, db.kept_tables(56))
    assert "'running','none')" in before_sql
    with pytest.raises(db.SchemaError, match="unchanged, still schema version 56"):
        db.open_db(path)
    with sqlite3.connect(path) as after:
        assert db.schema_version(after) == 56
        assert after.execute(q).fetchone()[0] == before_sql
        assert after.execute("PRAGMA writable_schema").fetchone()[0] == 0
        assert db.row_counts(after, db.kept_tables(56)) == counts
        assert_seeded(after)
        assert db.integrity_ok(after) is None
    with sqlite3.connect(path.with_name("old.db.v56.bak")) as backup:
        assert db.schema_version(backup) == 56
        assert backup.execute(q).fetchone()[0] == before_sql
        assert_seeded(backup)

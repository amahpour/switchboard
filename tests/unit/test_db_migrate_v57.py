"""Schema 57 (#907, DESIGN.md §37.11) lets a review board's reported CI be ``blocked``.

A pull request that conflicts with its base runs no CI. Only the ``pr_ci`` CHECK gains the value, by the
schema-text edit of v33 -> v34, so no board, item or foreign key moves.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def at_56(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 56)
        db.open_db(path).close()


def test_pr_ci_admits_blocked_and_still_refuses_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema 56 database refuses ci "blocked". After the step the boards and items are all there, the
    reviews table's SQL matches a fresh database's, "blocked" stores, an unknown value is still refused by
    the CHECK, and the backup is still schema 56 and refuses "blocked". Fails if the step is missing, the
    edit matches nothing, or the CHECK is dropped instead of widened (an unknown value would be stored)."""
    path = tmp_path / "old.db"
    at_56(path, monkeypatch)
    insert = "INSERT INTO reviews(room_id, url, opened_by, opened_at, pr_ci) VALUES (1, ?, 'alice', 1.0, ?)"
    with sqlite3.connect(path) as before:
        assert db.schema_version(before) == 56
        with pytest.raises(sqlite3.IntegrityError):
            before.execute(insert, ("https://example.com/a/pull/1", "blocked"))
        counts = db.row_counts(before, db.kept_tables(56))
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 57
    assert db.row_counts(con, db.kept_tables(56)) == counts
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
    fresh = db.open_db(tmp_path / "fresh.db")
    q = "SELECT sql FROM sqlite_master WHERE name='reviews'"
    assert con.execute(q).fetchone()[0] == fresh.execute(q).fetchone()[0]
    con.execute(insert, ("https://example.com/a/pull/1", "blocked"))
    for bad in ("conflicting", "Blocked"):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(insert, ("https://example.com/a/pull/2", bad))
    assert db.integrity_ok(con) is None
    with sqlite3.connect(path.with_name("old.db.v56.bak")) as backup:
        assert db.schema_version(backup) == 56
        with pytest.raises(sqlite3.IntegrityError):
            backup.execute(insert, ("https://example.com/a/pull/3", "blocked"))
    fresh.close()
    con.close()

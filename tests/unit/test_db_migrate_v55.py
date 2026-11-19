"""Schema 55 (#886): a review board may name the author's side. The step only adds the column,
empty on every board already stored."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_db_migrate_v54 import at_53

from switchboard import db

URL = "https://github.com/example/shop/pull/7"


def test_the_step_adds_author_side_empty_and_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """From 54, a checked backup, the old board kept, and ``author_side`` shaped like a fresh
    database's: empty, and no longer than a screen name. Fails if the step drops the row, the
    column differs from a fresh database, or a longer name is stored."""
    path = at_53(tmp_path, monkeypatch)
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 54)
        con = db.open_db(path)
        with db.tx(con):
            con.execute(
                "INSERT INTO reviews(room_id, url, opened_by, opened_at) VALUES(1, ?, 'codex-1', 1.0)",
                (URL,),
            )
        assert db.schema_version(con) == 54
        con.close()
    upgraded = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    backup = db.connect(path.with_name("old.db.v54.bak"), readonly=True)
    try:
        assert db.schema_version(upgraded) == db.SCHEMA_VERSION == 55
        assert db.schema_version(backup) == 54
        assert [tuple(r) for r in upgraded.execute("PRAGMA table_info(reviews)")] == [
            tuple(r) for r in fresh.execute("PRAGMA table_info(reviews)")
        ]
        rows = upgraded.execute("SELECT url, author_side FROM reviews ORDER BY id").fetchall()
        assert (URL, "") in [tuple(r) for r in rows]
        assert {r[1] for r in rows} == {""}
        assert db.integrity_ok(upgraded) is None
        with pytest.raises(sqlite3.IntegrityError):
            upgraded.execute("UPDATE reviews SET author_side=? WHERE url=?", ("x" * 25, URL))
    finally:
        backup.close()
        upgraded.close()
        fresh.close()

"""Schema 49 adds the voice a person pinned for a member on calls to a real old database (DESIGN.md §52.6)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def test_the_column_is_empty_for_every_old_member_and_its_shape_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release fixture, migrated to 48 first. Every membership keeps its rows and has no pinned voice
    (it gets its default from its name). The CHECK admits NULL and a well-shaped name, and refuses a path, a
    capital, a newline and 33 characters, so a bad value can't be stored even by a statement that skips the
    code. Fails if the shape is only checked in Python, or if old members lose rows."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 48)
        old = db.open_db(path)
        members = [tuple(r) for r in old.execute("SELECT * FROM memberships")]
        old.close()
    assert members, "the fixture has members to carry over"
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 49
    assert con.execute("SELECT COUNT(*) FROM memberships WHERE call_voice IS NULL").fetchone()[0] == len(
        members
    )
    assert [tuple(r)[: len(members[0])] for r in con.execute("SELECT * FROM memberships")] == members
    first = con.execute("SELECT id FROM memberships ORDER BY id LIMIT 1").fetchone()[0]
    for good in ("am_adam", "x", "a1_b2", "y" * 32):
        con.execute("UPDATE memberships SET call_voice=? WHERE id=?", (good, first))
    con.execute("UPDATE memberships SET call_voice=NULL WHERE id=?", (first,))
    for bad in ("", "../x", "Am_adam", "am adam", "a\n", "y" * 33):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE memberships SET call_voice=? WHERE id=?", (bad, first))
    fresh = db.open_db(tmp_path / "fresh.db")
    assert [tuple(r) for r in con.execute("PRAGMA table_info(memberships)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(memberships)")
    ]
    assert db.integrity_ok(con) is None
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    with sqlite3.connect(path.with_name("old.db.v48.bak")) as backup:
        assert db.schema_version(backup) == 48
        assert "call_voice" not in {r[1] for r in backup.execute("PRAGMA table_info(memberships)")}
    fresh.close()
    con.close()

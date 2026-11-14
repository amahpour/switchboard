"""Schema 50 adds the search index to a real old database and fills it from the messages (DESIGN.md §58)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db, search

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def at_49(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 49)
        db.open_db(path).close()


def test_the_index_is_built_from_the_old_messages_and_matches_a_fresh_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every chat line the old database holds is findable after the step, the messages themselves are
    untouched, the backup is schema 49 without the index, and the objects equal a fresh database's. Fails if
    the migration makes the index but does not fill it (search would find nothing old), if it counts the
    index as a table whose rows must be empty, or if the fresh and migrated schemas drift apart."""
    path = tmp_path / "old.db"
    at_49(path, monkeypatch)
    with sqlite3.connect(path) as before:
        assert before.execute("SELECT name FROM sqlite_master WHERE name='messages_fts'").fetchone() is None
        chat = before.execute(
            "SELECT id, text FROM messages WHERE kind='chat' AND sender_kind IN ('human','agent','meeting')"
        ).fetchall()
        total = before.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert chat, "the fixture has chat lines to index"
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 50
    assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == total
    indexed = {r[0] for r in con.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH '\"a\"*'")}
    assert indexed, "the backfill found words"
    for mid, text in chat:
        word = (search.words_of(text) or [None])[0]
        if word is None:
            continue
        hit = con.execute(
            "SELECT 1 FROM messages_fts WHERE messages_fts MATCH ? AND rowid=?",
            (search.match_query(word), mid),
        ).fetchone()
        assert hit is not None, f"message {mid} is not in the index"
    fresh = db.open_db(tmp_path / "fresh.db")
    q = (
        "SELECT type, name, sql FROM sqlite_master"
        " WHERE name LIKE '%fts%' OR name='search_source' ORDER BY name"
    )
    assert [tuple(r) for r in con.execute(q)] == [tuple(r) for r in fresh.execute(q)]
    assert con.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')") is not None
    assert db.integrity_ok(con) is None
    with sqlite3.connect(path.with_name("old.db.v49.bak")) as backup:
        assert db.schema_version(backup) == 49
        assert backup.execute("SELECT name FROM sqlite_master WHERE name='messages_fts'").fetchone() is None
    fresh.close()
    con.close()

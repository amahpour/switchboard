"""Schema 33 (#488) adds the listener keys of Claude Code's helper scripts (DESIGN.md §49.11).

The database is the real 0.37.2 one the schema-30 and 31 tests use (``tests/fixtures/db/v0_37_2.sql``,
schema 28, with three agent tokens), so the step runs after 29 to 32 in one migration, on rows that exist."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"


def migrated(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "switchboard.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    con.commit()
    con.close()
    os.chmod(path, 0o600)
    return db.open_db(path)


def test_an_older_database_gets_an_empty_table_of_keys_and_keeps_its_tokens(tmp_path: Path) -> None:
    """No key can exist before the plugin that makes them, so the table starts empty, and every token
    stays as it was. Fails if the step loses a token or leaves the table out of a migrated database."""
    con = migrated(tmp_path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 35
    assert con.execute("SELECT COUNT(*) FROM listener_keys").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM agent_tokens").fetchone()[0] == 3
    assert "listener_keys" in db.TABLES and "listener_keys" not in db.tables_of(32)
    con.close()


def test_a_migrated_database_and_a_fresh_one_have_the_same_table(tmp_path: Path) -> None:
    """The step and the fresh schema agree on the columns and the index, so a database that migrated
    behaves as one made new. Fails if either one is edited without the other."""
    con = migrated(tmp_path)
    fresh = db.open_db(tmp_path / "fresh.db")

    def shape(c: sqlite3.Connection) -> tuple[list[tuple[object, ...]], list[str]]:
        cols = [tuple(r)[1:] for r in c.execute("PRAGMA table_info(listener_keys)")]
        idx = sorted(r[1] for r in c.execute("PRAGMA index_list(listener_keys)") if r[3] == "c")
        return cols, idx

    assert shape(con) == shape(fresh)
    assert shape(fresh)[1] == ["listener_keys_token"]
    con.close()
    fresh.close()


def test_the_columns_say_what_they_may_hold(tmp_path: Path) -> None:
    """A key's hash is 64 characters and unique, its session at most 128, and it belongs to a token
    that exists, in the schema itself, so a bug in a store function can't write anything else. Fails if
    any of the three is missing from a migrated database."""
    con = migrated(tmp_path)
    good = ("a" * 64, 1, 1.0, 1.0)
    con.execute(
        "INSERT INTO listener_keys(key_hash, token_id, created_at, last_used_at) VALUES(?,?,?,?)", good
    )
    with pytest.raises(sqlite3.IntegrityError):  # the same hash twice
        con.execute(
            "INSERT INTO listener_keys(key_hash, token_id, created_at, last_used_at) VALUES(?,?,?,?)", good
        )
    with pytest.raises(sqlite3.IntegrityError):  # not a sha256
        con.execute(
            "INSERT INTO listener_keys(key_hash, token_id, created_at, last_used_at)"
            " VALUES('short',1,1.0,1.0)"
        )
    with pytest.raises(sqlite3.IntegrityError):  # a session id that long is not one
        con.execute(
            "INSERT INTO listener_keys(key_hash, token_id, session_id, created_at, last_used_at)"
            " VALUES(?,1,?,1.0,1.0)",
            ("b" * 64, "s" * 129),
        )
    with pytest.raises(sqlite3.IntegrityError):  # a token that doesn't exist
        con.execute(
            "INSERT INTO listener_keys(key_hash, token_id, created_at, last_used_at) VALUES(?,999,1.0,1.0)",
            ("c" * 64,),
        )
    con.close()

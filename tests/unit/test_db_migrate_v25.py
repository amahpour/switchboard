"""Schema 25 (#382) keeps every row and starts with no agent tokens."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_v24_upgrade_adds_an_empty_agent_tokens_table_after_a_backup(tmp_path: Path) -> None:
    """An upgrade keeps every row of a schema-24 database, adds an empty ``agent_tokens`` table with
    the fresh schema's columns and its one-live-token-per-device index, and backs the old file up
    first. Fails if the step is missing, or the table or index differs from a fresh database's."""
    path = tmp_path / "switchboard.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    for to, statements, _new in db._steps(3):
        if to == 25:
            break
        for statement in statements:
            con.execute(statement)
    assert db.schema_version(con) == 24
    con.commit()
    before = db.row_counts(con, db.kept_tables(24))
    con.close()
    os.chmod(path, 0o600)

    migrated = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    assert db.schema_version(migrated) == db.SCHEMA_VERSION == 31
    assert db.row_counts(migrated, db.kept_tables(24)) == before
    assert migrated.execute("SELECT COUNT(*) FROM agent_tokens").fetchone()[0] == 0
    assert [tuple(r) for r in migrated.execute("PRAGMA table_info(agent_tokens)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(agent_tokens)")
    ]

    def index_sql(c: sqlite3.Connection) -> str:
        return c.execute("SELECT sql FROM sqlite_master WHERE name='agent_tokens_device'").fetchone()[0]

    assert index_sql(migrated) == index_sql(fresh)
    assert path.with_name(path.name + ".v24.bak").exists()
    migrated.close()
    fresh.close()


def test_one_live_token_per_device_but_a_revoked_one_frees_the_name(tmp_path: Path) -> None:
    """The partial unique index lets a device name be used again once its token is revoked, and
    never twice while live. Fails if the index is missing (two live tokens for one device) or
    covers revoked rows too (a revoked token would hold its name for good)."""
    con = db.open_db(tmp_path / "s.db")
    insert = "INSERT INTO agent_tokens(person_id, device, token_hash, created_at) VALUES(?,?,?,?)"
    con.execute(insert, (None, "laptop", "a" * 64, 1.0))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(insert, (2, "laptop", "b" * 64, 2.0))
    con.execute("UPDATE agent_tokens SET revoked_at=3.0 WHERE device='laptop'")
    con.execute(insert, (2, "laptop", "b" * 64, 4.0))
    with pytest.raises(sqlite3.IntegrityError):  # a token's hash is unique, live or not
        con.execute(insert, (3, "desk", "a" * 64, 5.0))
    con.close()

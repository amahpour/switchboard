"""Schema 31 (#386) says how each agent token was made and adds the clients that register to sign an agent
in through the browser.

The database is the real 0.37.2 one the schema-30 test uses (``tests/fixtures/db/v0_37_2.sql``, schema 28,
with three agent tokens), so the step runs after 29 and 30 in one migration, on rows that exist."""

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


def test_every_token_of_an_older_database_stays_a_typed_one(tmp_path: Path) -> None:
    """Tokens made before browser sign-in were all typed in Settings, so they migrate as ``token`` with
    no client name, and keep every other column. Fails if a row is lost or changed, or if an old token
    would show as a sign-in."""
    con = migrated(tmp_path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 33
    rows = con.execute("SELECT * FROM agent_tokens ORDER BY id").fetchall()
    assert [(r["id"], r["device"], r["via"], r["client_name"]) for r in rows] == [
        (1, "laptop", "token", None),
        (2, "desktop", "token", None),
        (3, "old-box", "token", None),
    ]
    assert rows[2]["revoked_at"] == 1790000001.0 and rows[0]["revoked_at"] is None
    assert con.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 0
    assert "oauth_clients" in db.TABLES and "oauth_clients" not in db.tables_of(30)
    con.close()


def test_the_columns_say_what_they_may_hold(tmp_path: Path) -> None:
    """``via`` is one of two words and a client's name is at most 80 characters, in the schema itself,
    so a bug in a store function can't write anything else. Fails if either limit is missing from a
    migrated database."""
    con = migrated(tmp_path)
    ok = (9, "box", "f" * 64, 1.0)
    con.execute(
        "INSERT INTO agent_tokens(id, device, token_hash, created_at, via) VALUES(?,?,?,?, 'oauth')", ok
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO agent_tokens(id, device, token_hash, created_at, via) VALUES(10,'b2',?,1.0,'other')",
            ("e" * 64,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO agent_tokens(id, device, token_hash, created_at, client_name)"
            " VALUES(11,'b3',?,1.0,?)",
            ("d" * 64, "x" * 81),
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO oauth_clients VALUES('short', 'n', '[]', 1.0)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO oauth_clients VALUES('sbc_12345678', '', '[]', 1.0)")
    con.close()

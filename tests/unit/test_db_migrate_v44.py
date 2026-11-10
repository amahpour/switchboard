"""Schema 44: bind a script key to its harness as well as its session (DESIGN.md §49.11, #637)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def at_43(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Migrate the real old database only to 43, then leave a claimed Claude key in it."""
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 43)
        con = db.open_db(path)
        con.execute(
            "INSERT INTO agent_tokens(device, token_hash, created_at) VALUES('key-migration', ?, 1)",
            ("ab" * 32,),
        )
        token = con.execute("SELECT id FROM agent_tokens WHERE device='key-migration'").fetchone()[0]
        con.execute(
            "INSERT INTO listener_keys(token_id,key_hash,session_id,created_at,last_used_at)"
            " VALUES(?,?,?,1,1)",
            (token, "cd" * 32, "thread-1"),
        )
        con.close()


def test_old_keys_remain_claudes_and_both_schemas_constrain_the_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration preserves old keys and their bindings, defaults them to Claude, adds a constrained
    harness column matching a fresh DB, and keeps a schema-43 backup. Unknown harnesses fail closed."""
    path = tmp_path / "old.db"
    at_43(path, monkeypatch)
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 48
    row = con.execute("SELECT key_hash,session_id,harness FROM listener_keys").fetchone()
    assert tuple(row) == ("cd" * 32, "thread-1", "claude")
    fresh = db.open_db(tmp_path / "new.db")
    assert [tuple(r) for r in con.execute("PRAGMA table_info(listener_keys)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(listener_keys)")
    ]
    for bad in ("cursor", "", None):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE listener_keys SET harness=?", (bad,))
    con.execute("UPDATE listener_keys SET harness='codex'")
    assert db.integrity_ok(con) is None
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    con.close()
    fresh.close()
    with sqlite3.connect(path.with_name("old.db.v43.bak")) as backup:
        assert db.schema_version(backup) == 43
        assert "harness" not in [r[1] for r in backup.execute("PRAGMA table_info(listener_keys)")]
        assert backup.execute("SELECT session_id FROM listener_keys").fetchone()[0] == "thread-1"

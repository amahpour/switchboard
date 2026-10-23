"""Schema 34 admits OpenCode without rebuilding participants or its foreign keys (§49.12, #513)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def test_old_rows_and_constraints_survive_and_opencode_can_join(tmp_path: Path) -> None:
    """A real old database keeps every surviving row and FK, admits OpenCode, and refuses unknown
    harnesses."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(FIXTURE.read_text())
    old.commit()
    counts = db.row_counts(old, db.kept_tables(28))
    old.close()
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 34
    assert db.row_counts(con, db.kept_tables(28)) == counts
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
    for harness in ("opencode", "not-a-harness"):
        sql = "INSERT INTO participants(harness,session_key,created_at) VALUES(?,?,1.0)"
        if harness == "opencode":
            con.execute(sql, (harness, "opencode@laptop:ses_1"))
        else:
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(sql, (harness, "not-a-harness:1"))
    con.close()
    again = db.open_db(path)
    assert again.execute("SELECT count(*) FROM participants WHERE harness='opencode'").fetchone()[0] == 1
    again.close()
    backup = sqlite3.connect(path.with_name("old.db.v28.bak"))
    assert db.schema_version(backup) == 28
    backup.close()


def test_failed_enum_migration_rolls_back_and_turns_schema_editing_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure while editing the CHECK restores schema 33, its rows and normal schema protections."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(FIXTURE.read_text())
    old.commit()
    old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 33)
        at_33 = db.open_db(path)
        at_33.close()
    con = db.connect(path)
    counts = db.row_counts(con, db.TABLES)
    monkeypatch.setattr(db, "V33_TO_V34", (*db.V33_TO_V34, "SELECT missing_column FROM participants"))
    try:
        with pytest.raises(db.SchemaError, match="unchanged, still schema version 33"):
            db.migrate(con, backup_to=tmp_path / "before-34.db")
        assert db.schema_version(con) == 33
        assert db.row_counts(con, db.TABLES) == counts
        assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.integrity_ok(con) is None
    finally:
        con.close()

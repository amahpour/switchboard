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
    assert db.schema_version(con) == db.SCHEMA_VERSION == 35
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


def test_unmatched_check_fails_closed_with_v33_and_its_backup_intact(tmp_path: Path) -> None:
    """An equivalent CHECK with different spacing cannot silently skip the enum update.
    A failed match must roll back schema 34, preserve all rows and keep the verified v33 backup.
    """
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(FIXTURE.read_text())
    old.commit()
    old.close()
    # Build the real pre-34 schema, with the old constraint spelled differently but equally strict.
    con = db.connect(path)
    original_version = db.SCHEMA_VERSION
    try:
        db.SCHEMA_VERSION = 33
        db.migrate(con, backup_to=tmp_path / "before-33.db")
    finally:
        db.SCHEMA_VERSION = original_version
    con.execute("PRAGMA writable_schema=ON")
    con.execute(
        "UPDATE sqlite_master SET sql=replace(sql, '''claude'',''codex''', '''claude'', ''codex''')"
        " WHERE type='table' AND name='participants'"
    )
    con.execute("PRAGMA writable_schema=RESET")
    before_sql = con.execute("SELECT sql FROM sqlite_master WHERE name='participants'").fetchone()[0]
    counts = db.row_counts(con, db.TABLES)
    backup = tmp_path / "before-34.db"
    try:
        with pytest.raises(db.SchemaError, match="opencode.*unchanged, still schema version 33"):
            db.migrate(con, backup_to=backup)
        assert db.schema_version(con) == 33
        assert (
            con.execute("SELECT sql FROM sqlite_master WHERE name='participants'").fetchone()[0] == before_sql
        )
        assert db.row_counts(con, db.TABLES) == counts
        assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.integrity_ok(con) is None
        with sqlite3.connect(backup) as saved:
            assert db.schema_version(saved) == 33
            assert db.row_counts(saved, db.TABLES) == counts
            assert (
                saved.execute("SELECT sql FROM sqlite_master WHERE name='participants'").fetchone()[0]
                == before_sql
            )
            assert db.integrity_ok(saved) is None
    finally:
        con.close()

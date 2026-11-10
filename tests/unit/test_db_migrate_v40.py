"""Schema 40: files a room holds, and the broker's settings (#544, DESIGN.md §53).

The database is the real 0.37.2 one, migrated to 39 first, so the new tables are the
step under test. The backup written first is that schema-39 file, which has no
``files``, ``file_grants`` or ``broker_settings``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"


def at_39(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = sqlite3.connect(path)
    old.executescript(FIXTURE.read_text())
    old.commit()
    old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 39)
        con = db.open_db(path)
        assert db.schema_version(con) == 39
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "files" not in tables and "file_grants" not in tables and "broker_settings" not in tables
        con.close()


def test_files_and_settings_and_the_backup_is_schema_39(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file row is stored, a bad id or state is refused, and every older row survives.
    The backup is the schema-39 database, without the new tables. Fails if the step drops
    a row, or the new tables are missing or unconstrained."""
    path = tmp_path / "old.db"
    at_39(path, monkeypatch)
    before = sqlite3.connect(path)
    counts = db.row_counts(before, db.tables_of(39))
    room = before.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
    before.close()

    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 48
    assert db.row_counts(con, db.tables_of(39)) == counts
    assert db.row_counts(con, db.TABLES)["files"] == 0
    assert db.row_counts(con, db.TABLES)["file_grants"] == 0
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    assert db.integrity_ok(con) is None
    file_id = "ab" * 16
    con.execute(
        "INSERT INTO files(id, room_id, name, size, sha256, content_type, uploader_name,"
        " uploader_kind, state, created_at) VALUES(?, ?, 'notes.txt', 5, ?, '', 'alice', 'human',"
        " 'stored', 1.0)",
        (file_id, room, "cd" * 32),
    )
    con.execute("INSERT INTO broker_settings(key, value) VALUES('files_retention_days', '30')")
    con.execute(
        "INSERT INTO file_grants(token_hash, file_id, kind, expires_at) VALUES(?, ?, 'upload', 2.0)",
        ("ef" * 32, file_id),
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO files(id, room_id, name, content_type, uploader_name, uploader_kind,"
            " state, created_at) VALUES('not-hex', ?, 'x', '', 'alice', 'human', 'stored', 1.0)",
            (room,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO files(id, room_id, name, content_type, uploader_name, uploader_kind,"
            " state, created_at) VALUES(?, ?, 'x', '', 'alice', 'nope', 'stored', 1.0)",
            ("12" * 16, room),
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO files(id, room_id, name, content_type, uploader_name, uploader_kind,"
            " state, created_at) VALUES(?, ?, 'x', '', 'alice', 'human', 'gone', 1.0)",
            ("34" * 16, room),
        )
    con.close()

    backup = sqlite3.connect(path.with_name("old.db.v39.bak"))
    assert db.schema_version(backup) == 39
    tables = {r[0] for r in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "files" not in tables and "file_grants" not in tables and "broker_settings" not in tables
    assert db.row_counts(backup, db.tables_of(39)) == counts
    backup.close()

"""Schema 41: a file can be uploaded by the call itself (#593, DESIGN.md §52).

``files.uploader_kind`` gains ``meeting``, for a snapshot a line on the call asked for. The
database is the real 0.37.2 one, migrated to 40 first, with a file row from a person and one
from an agent, so the CHECK edit is the step under test. The backup written first is that
schema-40 file, with the old CHECK.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"
INSERT = (
    "INSERT INTO files(id, room_id, name, size, sha256, content_type, uploader_name,"
    " uploader_kind, state, created_at) VALUES(?, ?, 'screen.png', 5, ?, '', ?, ?, 'stored', 1.0)"
)


def at_40(path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """A schema-40 database holding one person's file and one agent's. Returns a room id."""
    old = sqlite3.connect(path)
    old.executescript(FIXTURE.read_text())
    old.commit()
    old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 40)
        con = db.open_db(path)
        assert db.schema_version(con) == 40
        room = con.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
        con.execute(INSERT, ("ab" * 16, room, "cd" * 32, "alice", "human"))
        con.execute(INSERT, ("12" * 16, room, "ef" * 32, "claude-1", "agent"))
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(INSERT, ("34" * 16, room, "cd" * 32, "call", "meeting"))
        con.commit()
        con.close()
    return int(room)


def test_a_file_may_come_from_the_call_and_older_files_and_the_backup_are_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file uploaded by ``meeting`` is stored, an unknown uploader is refused, both older files
    survive with their uploader, and the backup is the schema-40 database with the old CHECK.
    Fails if the step leaves the CHECK as it was, or rebuilds ``files`` and drops a row."""
    path = tmp_path / "old.db"
    room = at_40(path, monkeypatch)
    before = sqlite3.connect(path)
    counts = db.row_counts(before, db.TABLES)
    assert counts["files"] == 2
    before.close()

    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 44
    assert db.row_counts(con, db.TABLES) == counts
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    assert db.integrity_ok(con) is None
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='files'").fetchone()[0]
    assert "'human','agent','meeting'" in sql
    kinds = dict(con.execute("SELECT id, uploader_kind FROM files"))
    assert kinds == {"ab" * 16: "human", "12" * 16: "agent"}
    con.execute(INSERT, ("34" * 16, room, "cd" * 32, "call", "meeting"))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(INSERT, ("56" * 16, room, "cd" * 32, "call", "nope"))
    con.close()

    backup = sqlite3.connect(path.with_name("old.db.v40.bak"))
    assert db.schema_version(backup) == 40
    old_sql = backup.execute("SELECT sql FROM sqlite_master WHERE name='files'").fetchone()[0]
    assert "'meeting'" not in old_sql
    assert db.row_counts(backup, db.TABLES) == counts
    backup.close()


def test_a_new_database_allows_the_call_as_an_uploader(tmp_path: Path) -> None:
    """The CREATE a new database starts from says the same as the migrated one, so a fresh
    broker and an upgraded one accept the same rows. Fails if only the migration was edited."""
    con = db.open_db(tmp_path / "new.db")
    room = con.execute(
        "INSERT INTO rooms(name, created_at, created_by, budget_per_hour, budget_remaining,"
        " budget_window_start, hop_limit) VALUES('#a', 1.0, 'alice', 60, 60, 1.0, 30)"
    ).lastrowid
    con.execute(INSERT, ("ab" * 16, room, "cd" * 32, "call", "meeting"))
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='files'").fetchone()[0]
    assert "'human','agent','meeting'" in sql
    con.close()

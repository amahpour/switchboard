"""Schema 39: a call in a room, and a meeting line in the chat (#573, DESIGN.md §52).

``messages.sender_kind`` and ``messages.via`` gain ``meeting``. A ``meetings`` row is
the call (joining, live, ended), one open per room. The database is the real 0.37.2
one, migrated to 38 first, so the CHECK edit is the step under test. The backup
written first is that schema-38 file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"


def at_38(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = sqlite3.connect(path)
    old.executescript(FIXTURE.read_text())
    old.commit()
    old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 38)
        con = db.open_db(path)
        assert db.schema_version(con) == 38
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        assert "'meeting'" not in sql
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "meetings" not in tables
        con.close()


def test_meeting_lines_and_the_meetings_table_and_the_backup_is_schema_38(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A meeting line is stored, an unknown kind is refused, one room has one open call,
    and every older row survives. The backup is the schema-38 database, with the old
    CHECKs and no meetings table. Fails if the step rebuilds messages and drops a row,
    leaves the CHECK as it was, or allows two open calls in one room."""
    path = tmp_path / "old.db"
    at_38(path, monkeypatch)
    before = sqlite3.connect(path)
    counts = db.row_counts(before, db.tables_of(38))
    room = before.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
    before.close()

    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 42
    assert db.row_counts(con, db.tables_of(38)) == counts
    assert db.row_counts(con, db.TABLES)["meetings"] == 0
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    assert db.integrity_ok(con) is None
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
    assert "'human','agent','system','meeting'" in sql
    assert "'web','cli','mcp','system','meeting'" in sql
    con.execute(
        "INSERT INTO messages(room_id, ts, sender_name, sender_kind, via, text)"
        " VALUES(?, ?, 'call', 'meeting', 'meeting', 'hello')",
        (room, 1.0),
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO messages(room_id, ts, sender_name, sender_kind, via, text)"
            " VALUES(?, ?, 'call', 'nope', 'meeting', 'x')",
            (room, 1.0),
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO messages(room_id, ts, sender_name, sender_kind, via, text)"
            " VALUES(?, ?, 'call', 'meeting', 'nope', 'x')",
            (room, 1.0),
        )
    con.execute(
        "INSERT INTO meetings(room_id, state, started_at, started_by, platform)"
        " VALUES(?, 'live', 1.0, 'alice', 'zoom')",
        (room,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO meetings(room_id, state, started_at, started_by, platform)"
            " VALUES(?, 'joining', 2.0, 'alice', 'meet')",
            (room,),
        )
    con.execute("UPDATE meetings SET state='ended', ended_at=3.0 WHERE room_id=?", (room,))
    con.execute(
        "INSERT INTO meetings(room_id, state, started_at, started_by, platform)"
        " VALUES(?, 'joining', 4.0, 'alice', 'teams')",
        (room,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO meetings(room_id, state, started_at, started_by, platform)"
            " VALUES(?, 'live', 5.0, 'alice', 'nope')",
            (room,),
        )
    con.close()

    backup = sqlite3.connect(path.with_name("old.db.v38.bak"))
    assert db.schema_version(backup) == 38
    old_sql = backup.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
    assert "'meeting'" not in old_sql
    tables = {r[0] for r in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "meetings" not in tables
    assert db.row_counts(backup, db.tables_of(38)) == counts
    backup.close()

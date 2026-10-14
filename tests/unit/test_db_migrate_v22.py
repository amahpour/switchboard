"""Schema 22 (#292) adds a room's pinned decisions and keeps every existing row. Schema 21 is
#293's (advisory claims)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def database_at(path: Path, version: int) -> sqlite3.Connection:
    """A database from the 0.7.0 fixture, stepped up to ``version`` and holding one message."""
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    for to, statements, _new in db._steps(3):
        if to > version:
            break
        for statement in statements:
            con.execute(statement)
    assert db.schema_version(con) == version
    return con


@pytest.mark.parametrize("version", [20, 21])
def test_an_older_database_gains_an_empty_pins_table_after_a_backup(tmp_path: Path, version: int) -> None:
    """The step from 20 goes through 21 (#293's claims). The messages and rooms are as they were, the
    pins table is empty and has the fresh schema's columns, and the backup of the old file is made.
    Fails if the chain skips a version, forgets to create the table, or counts it in a database
    that doesn't have it yet."""
    path = tmp_path / "switchboard.db"
    con = database_at(path, version)
    before = db.row_counts(con, db.tables_of(version))
    assert "pins" not in db.tables_of(version)
    assert db.tables_of(version)[-1] == ("room_people" if version == 20 else "claims")
    con.commit()
    con.close()
    os.chmod(path, 0o600)

    migrated = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    assert db.schema_version(migrated) == db.SCHEMA_VERSION == 22
    assert db.row_counts(migrated, db.tables_of(version)) == before
    assert migrated.execute("SELECT COUNT(*) FROM pins").fetchone()[0] == 0
    assert [tuple(r) for r in migrated.execute("PRAGMA table_info(pins)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(pins)")
    ]
    assert db.tables_of(22)[-1] == "pins"
    assert path.with_name(path.name + f".v{version}.bak").exists()
    migrated.close()
    fresh.close()


def test_a_pin_needs_its_room_and_message_and_is_unique_per_message(tmp_path: Path) -> None:
    con = db.open_db(tmp_path / "switchboard.db")
    con.execute("PRAGMA foreign_keys=ON")
    insert = "INSERT INTO pins(room_id, message_id, pinned_by, pinned_at) VALUES(?,?,?,?)"
    with pytest.raises(sqlite3.IntegrityError):  # no such room or message
        con.execute(insert, (1, 1, "alice", 1.0))
    con.close()

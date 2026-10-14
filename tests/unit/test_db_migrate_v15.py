"""The v14 to v15 migration (#74, DESIGN.md §44): rooms gain ``private``, 0 for every room that
exists, so nobody loses sight of one, and an empty ``room_people`` table; every row stays,
after a checked backup."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_rooms_stay_public_and_gain_an_empty_people_table_with_a_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    steps = (*db.V3_TO_V4, *db.V4_TO_V5, *db.V5_TO_V6, *db.V6_TO_V7, *db.V7_TO_V8, *db.V8_TO_V9)
    for stmt in (*steps, *db.V9_TO_V10, *db.V10_TO_V11, *db.V11_TO_V12, *db.V12_TO_V13, *db.V13_TO_V14):
        con.execute(stmt)
    con.commit()
    assert db.schema_version(con) == 14
    rooms = con.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
    assert rooms > 0  # the fixture has rooms, so "they stay public" means something
    before = db.row_counts(con, db.V14_TABLES)
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 22
    assert db.row_counts(con, db.V14_TABLES) == before
    assert con.execute("SELECT COUNT(*) FROM rooms WHERE private=0").fetchone()[0] == rooms
    assert con.execute("SELECT COUNT(*) FROM room_people").fetchone()[0] == 0
    assert p.with_name(p.name + ".v14.bak").exists()
    con.close()


def test_a_private_room_has_its_creator_from_the_start(tmp_path: Path) -> None:
    from conftest import FakeClock

    from switchboard.store import Store

    store = Store(db.open_db(tmp_path / "y.db"), FakeClock())
    secret = store.create_room("#secret", "bob", 60, 6, private_for=3)
    open_ = store.create_room("#open", "bob", 60, 6)
    assert (secret.private, open_.private) == (True, False)
    assert store.room_people(secret.id) == [3] and store.room_people(open_.id) == []
    assert store.room_person_add(secret.id, 0, 3) and not store.room_person_add(secret.id, 0, 3)
    assert store.room_people(secret.id) == [3, 0] and store.rooms_of_person(0) == [secret.id]
    assert store.room_person_remove(secret.id, 3) and not store.room_person_remove(secret.id, 3)
    assert store.room_people(secret.id) == [0]

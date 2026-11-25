"""Schema 62 turns the old built-in wake budget off (#965, DESIGN.md §42.2). The step from 61 moves the
rooms whose ``budget_per_hour`` is 60, the old default, to 0 (no limit), with what is left this hour,
and touches nothing else. It runs on ``v1_14_0.sql``, a real database 1.14.0 wrote
(``tests/fixtures/db/README.md``), brought to schema 62 through every step between."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v1_14_0.sql"
ROOMS = (
    "SELECT name, budget_per_hour, budget_remaining, budget_window_start, hop_limit FROM rooms ORDER BY id"
)
NEW_ROOM = (
    "INSERT INTO rooms(name, created_at, created_by, budget_per_hour, budget_remaining,"
    " budget_window_start, hop_limit) VALUES(?, 1790000000.0, 'alice', ?, ?, 1790000000.0, 6)"
)


def old_database(path: Path) -> None:
    """The 1.14.0 database, plus a room at 10 wakes (3 left), one at 100, and a person's own defaults."""
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
        old.execute(NEW_ROOM, ("#small", 10, 3))
        old.execute(NEW_ROOM, ("#big", 100, 100))
        old.execute(NEW_ROOM, ("#spent", 60, 0))
        old.execute("INSERT INTO preferences(person_id, budget_per_hour, hop_limit) VALUES(1, 60, 6)")
        old.execute("INSERT INTO preferences(person_id) VALUES(2)")


def test_rooms_at_the_old_default_move_to_no_limit_and_the_rest_keep_theirs(tmp_path: Path) -> None:
    """A room at 60 an hour (the old built-in default, with or without wakes left) becomes 0 and 0, a room
    at 10 or 100 keeps its rate and what is left, a person's own default of 60 stays 60 and an unset one
    stays unset, and no row is added or lost. The backup is schema 61 and still has the 60s. Fails if the
    step leaves a 60 alone, moves any other number, leaves the count above the rate, or rewrites
    ``preferences``."""
    path = tmp_path / "old.db"
    old_database(path)
    with sqlite3.connect(path) as before:
        assert db.schema_version(before) == 60
        counts = db.row_counts(before, db.TABLES)
        prefs = [tuple(r) for r in before.execute("SELECT * FROM preferences ORDER BY person_id")]
    con = db.open_db(path)
    backup = db.connect(db.backup_path_for(path, 60), readonly=True)
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION == 62
        assert [tuple(r) for r in con.execute(ROOMS)] == [
            ("#build", 0, 0, 1790000000.0, 15),
            ("#small", 10, 3, 1790000000.0, 6),
            ("#big", 100, 100, 1790000000.0, 6),
            ("#spent", 0, 0, 1790000000.0, 6),
        ]
        assert [tuple(r) for r in con.execute("SELECT * FROM preferences ORDER BY person_id")] == prefs
        assert db.row_counts(con, db.TABLES) == counts
        assert [r[0] for r in backup.execute("SELECT budget_per_hour FROM rooms ORDER BY id")] == [
            60,
            10,
            100,
            60,
        ]
        assert db.integrity_ok(con) is None
    finally:
        backup.close()
        con.close()


def test_a_migrated_room_is_never_held_back_by_a_budget(tmp_path: Path) -> None:
    """After the step a room that was at 60 reads as having no budget: ``budget_on`` is false, so nothing
    treats its count of 0 as used up. Fails if the room keeps a limit of 0 wakes."""
    path = tmp_path / "old.db"
    old_database(path)
    from switchboard.store import Store

    con = db.open_db(path)
    try:
        store = Store(con)
        build = store.room_by_id(1)
        assert build is not None
        assert not build.budget_on and not build.budget_spent
        small = store.room_by_id(2)
        assert small is not None and small.budget_on and not small.budget_spent
    finally:
        con.close()

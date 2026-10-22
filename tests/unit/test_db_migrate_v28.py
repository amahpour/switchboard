"""Schema 28 (#445) keeps closed boards and names every review event's board, and keeps every row."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.store import Store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"
URL_A = "https://example.com/shop/pull/7"
URL_B = "https://example.com/shop/pull/8"


def database_at_27(path: Path) -> sqlite3.Connection:
    """A database from the 0.7.0 fixture stepped up to schema 27, with the history of a room that
    kept one board at a time: board A (opened at 1, closed at 5), then board B (opened at 6, still
    open), and review events from both with no board named, as they were stored before this change.
    Also an event of another kind, one in a room with no board, an older ``reviewer`` event and a
    review event that is not JSON, none of which a migration may touch or choke on."""
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    for to, statements, _new in db._steps(3):
        if to == 28:
            break
        for statement in statements:
            con.execute(statement)
    assert db.schema_version(con) == 27
    room_id, other_room = (r[0] for r in con.execute("SELECT id FROM rooms ORDER BY id LIMIT 2"))
    con.execute(
        "INSERT INTO reviews(room_id,url,head,opened_by,opened_at,closed_at) VALUES(?,?,?,?,?,?)",
        (room_id, URL_A, "aaa1111", "codex-1", 1.0, 5.0),
    )
    con.execute(
        "INSERT INTO reviews(room_id,url,head,opened_by,opened_at) VALUES(?,?,?,?,?)",
        (room_id, URL_B, "bbb2222", "claude-1", 6.0),
    )
    con.execute(
        "INSERT INTO review_items(review_id,kind,n,state,title,raised_by,created_at,updated_at)"
        " VALUES(1,'finding',1,'raised','a finding on A','codex-1',2.0,2.0)"
    )

    def event(ts: float, room: int | None, kind: str, data: str) -> None:
        con.execute("INSERT INTO events(ts,room_id,kind,data) VALUES(?,?,?,?)", (ts, room, kind, data))

    event(2.0, room_id, "review", json.dumps({"item": "F1", "to": "conceded", "by": "claude-1"}))
    event(
        5.5, room_id, "review", json.dumps({"close": URL_A, "by": "codex-1"})
    )  # a close lands just after closed_at
    event(7.0, room_id, "review", json.dumps({"item": "F1", "to": "conceded", "by": "codex-1"}))
    event(
        0.5, room_id, "review", json.dumps({"item": "F9", "to": "dropped", "by": "alice"})
    )  # before any board
    event(
        8.0, other_room, "review", json.dumps({"item": "F1", "to": "dropped", "by": "alice"})
    )  # no board there
    event(8.5, room_id, "review", json.dumps({"reviewer": "codex-1", "target": "claude-1"}))  # before #248
    event(9.0, room_id, "review", "not json")
    event(9.5, room_id, "join", json.dumps({"who": "claude-1"}))
    con.commit()
    return con


def stamped(con: sqlite3.Connection) -> list[tuple[float, object]]:
    """Each review event's ``board`` as the migration left it (None when it has none), oldest first."""
    out = []
    for ts, data in con.execute("SELECT ts, data FROM events WHERE kind='review' ORDER BY ts"):
        try:
            out.append((ts, json.loads(data).get("board")))
        except ValueError:
            out.append((ts, "not json"))
    return out


def test_a_v27_database_keeps_its_rows_and_each_review_event_names_the_board_it_belonged_to(
    tmp_path: Path,
) -> None:
    """An upgrade keeps every row, adds the three columns as a fresh database has them, and backs the
    old file up first. Every event of a board's moves is stamped with the board that was newest in its
    room when it was written (the room's only one then): so A's history stays A's and B's stays B's,
    though both boards number their findings F1. An event before any board, in a room with none, an
    older ``reviewer`` event, one that is not JSON and one of another kind are left exactly as they
    were. Fails if the step is missing, a column differs from a fresh database's, an event lands on
    the wrong board, or a stray row stops the migration."""
    path = tmp_path / "switchboard.db"
    con = database_at_27(path)
    before = db.row_counts(con, db.kept_tables(27))
    con.close()
    os.chmod(path, 0o600)

    migrated = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    assert db.schema_version(migrated) == db.SCHEMA_VERSION == 32
    assert db.row_counts(migrated, db.kept_tables(27)) == before
    assert [tuple(r) for r in migrated.execute("PRAGMA table_info(reviews)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(reviews)")
    ]
    assert path.with_name(path.name + ".v27.bak").exists()
    rows = migrated.execute(
        "SELECT id, closed_at, closed_by, reopened_by, reopened_at FROM reviews"
    ).fetchall()
    assert [tuple(r) for r in rows] == [(1, 5.0, "", "", None), (2, None, "", "", None)]
    # A's own moves, with the close that lands just after closed_at, then B's; the strays untouched
    assert stamped(migrated) == [
        (0.5, None), (2.0, 1), (5.5, 1), (7.0, 2), (8.0, None), (8.5, None), (9.0, "not json"),
    ]  # fmt: skip
    assert migrated.execute("SELECT data FROM events WHERE kind='join'").fetchone()[0] == json.dumps(
        {"who": "claude-1"}
    )
    migrated.close()
    fresh.close()

    store = Store(db.open_db(path), FakeClock())
    room_id = store.con.execute("SELECT room_id FROM reviews WHERE id=1").fetchone()[0]
    assert [e.data["by"] for e in store.review_events(room_id, 1, 1.0)] == ["claude-1", "codex-1"]
    assert [e.data["by"] for e in store.review_events(room_id, 2, 6.0)] == ["codex-1"]  # not A's F1 move


def test_the_new_columns_are_bounded_and_a_board_closes_and_comes_back_in_the_store(tmp_path: Path) -> None:
    """``closed_by`` and ``reopened_by`` hold a name (at most 100 characters, the last line), closing
    a board records who, and ``reopen_review`` clears the close and records who opened it again.
    Fails if a column is unbounded, or a reopen keeps the old close."""
    store = Store(db.open_db(tmp_path / "s.db"), FakeClock())
    room = store.create_room("#build", "alice", 60, 30)
    board = store.open_review(room.id, URL_A, "", "codex-1")
    store.set_review(board["id"], closed_at=5.0, closed_by="claude-1")
    got = store.review_for_url(room.id, URL_A)
    assert got is not None and (got["closed_at"], got["closed_by"]) == (5.0, "claude-1")
    store.reopen_review(board["id"], "devin-1")
    got = store.review_for_url(room.id, URL_A)
    assert got is not None
    assert (got["closed_at"], got["closed_by"], got["reopened_by"]) == (None, "", "devin-1")
    assert got["reopened_at"] is not None
    for column in ("closed_by", "reopened_by"):
        with pytest.raises(sqlite3.IntegrityError):
            store.con.execute(f"UPDATE reviews SET {column}=? WHERE id=?", ("x" * 101, board["id"]))

"""Schema 42: a call can be hosted on a person's machine, and its lines are stored with it (#623, §54).

``meetings`` gains the source (the broker's own bot or a hosted call), the sha256 of the call's key, the
token that opened it and its person, the machine's label, the model and the count of lines pushed;
``platform`` also admits ``unknown``; ``messages`` gains ``meeting_id``. The database is the real 0.37.2 one,
migrated to 41 first, with a call of the broker's own and a line of it, so the step under test is the new
one. The backup written first is that schema-41 file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"
HOSTED = (
    "INSERT INTO meetings(room_id, state, started_at, started_by, platform, source, key_hash, token_id,"
    " owner_person_id, host, model) VALUES(?, 'joining', 5.0, 'alice', ?, ?, ?, ?, NULL, ?, ?)"
)
TOKEN = (
    "INSERT INTO agent_tokens(person_id, device, token_hash, created_at) VALUES(NULL, 'alice-laptop', ?, 1.0)"
)
GOOD = ("zoom", "hosted", "ab" * 32, 1, "alice-laptop", "large-v3-turbo")


def at_41(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    """A schema-41 database with one finished call of the broker's own and one line of it. Returns the room's
    id and that call's."""
    old = sqlite3.connect(path)
    old.executescript(FIXTURE.read_text())
    old.commit()
    old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 41)
        con = db.open_db(path)
        assert db.schema_version(con) == 41
        room = con.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
        con.execute(
            "INSERT INTO meetings(room_id, state, started_at, started_by, platform, ended_at)"
            " VALUES(?, 'ended', 1.0, 'alice', 'zoom', 2.0)",
            (room,),
        )
        call = con.execute("SELECT id FROM meetings").fetchone()[0]
        con.execute(
            "INSERT INTO messages(room_id, ts, sender_name, sender_kind, via, kind, text)"
            " VALUES(?, 1.5, 'bob', 'meeting', 'meeting', 'chat', 'hello from the call')",
            (room,),
        )
        with pytest.raises(sqlite3.IntegrityError):  # before the step, a platform can't be unknown
            con.execute(
                "INSERT INTO meetings(room_id, state, started_at, started_by, platform)"
                " VALUES(?, 'joining', 3.0, 'alice', 'unknown')",
                (room,),
            )
        con.commit()
        con.close()
    return int(room), int(call)


def test_the_old_call_and_line_are_kept_as_the_brokers_own_and_a_hosted_call_can_be_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every call that was there is the broker's own, with no key, token or host, and its line has no call
    of its own. A hosted call (unknown platform too) is then stored with its key's hash, token and counts.
    Fails if the step loses a row, gives an old call a source it didn't have, or leaves the platform's CHECK
    as it was."""
    path = tmp_path / "old.db"
    room, call = at_41(path, monkeypatch)
    before = sqlite3.connect(path)
    counts = db.row_counts(before, db.TABLES)
    assert counts["meetings"] == 1 and counts["messages"] >= 1
    before.close()

    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 42
    assert db.row_counts(con, db.TABLES) == counts
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    assert db.integrity_ok(con) is None
    old = con.execute("SELECT * FROM meetings WHERE id=?", (call,)).fetchone()
    assert (old["state"], old["platform"], old["source"], old["lines_pushed"]) == (
        "ended",
        "zoom",
        "server",
        0,
    )
    assert all(old[k] is None for k in ("key_hash", "token_id", "owner_person_id", "host", "model"))
    line = con.execute(
        "SELECT meeting_id, sender_host FROM messages WHERE text LIKE 'hello from%'"
    ).fetchone()
    assert tuple(line) == (None, None)

    con.execute(TOKEN, ("0" * 64,))
    con.execute(HOSTED, (room, *GOOD))
    hosted = con.execute("SELECT id FROM meetings WHERE source='hosted'").fetchone()[0]
    con.execute("UPDATE meetings SET state='ended' WHERE id=?", (hosted,))
    con.execute(HOSTED, (room, "unknown", "hosted", "cd" * 32, 1, "alice-laptop", None))  # no platform
    con.execute(
        "INSERT INTO messages(room_id, ts, sender_name, sender_kind, via, kind, text, meeting_id,"
        " sender_host) VALUES(?, 6.0, 'bob', 'meeting', 'meeting', 'chat', 'pushed', ?, 'alice-laptop')",
        (room, hosted),
    )
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    con.close()


@pytest.mark.parametrize(
    "why, change",
    [
        ("a platform this code does not know", {"platform": "webex"}),
        ("a source this code does not know", {"source": "relay"}),
        ("a key hash that is not a sha256", {"key": "ab" * 31}),
        ("a host name past 24 characters", {"host": "h" * 25}),
        ("an empty host name", {"host": ""}),
        ("a model name past 80 characters", {"model": "m" * 81}),
        ("a token that does not exist", {"token": 999}),
    ],
)
def test_the_new_columns_refuse_what_the_broker_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, why: str, change: dict[str, object]
) -> None:
    """The columns are constrained in the database too, so a bug in the code that writes them can't leave a
    row the code that reads them would have to guess at. Fails if one of the constraints is missing."""
    path = tmp_path / "old.db"
    room, _call = at_41(path, monkeypatch)
    con = db.open_db(path)
    con.execute(TOKEN, ("0" * 64,))
    names = ("platform", "source", "key", "token", "host", "model")
    given = {**dict(zip(names, GOOD, strict=True)), **change}
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(HOSTED, (room, *(given[n] for n in names)))
    con.close()


def test_a_key_is_unique_and_a_call_counts_its_lines_only_upward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One key names one call ever (a unique index on the hash, which NULLs, the broker's own calls', don't
    share), and the count of lines can't go below zero. Fails if two calls can share a key."""
    path = tmp_path / "old.db"
    room, _call = at_41(path, monkeypatch)
    con = db.open_db(path)
    con.execute(TOKEN, ("0" * 64,))
    con.execute(HOSTED, (room, *GOOD))
    con.execute("UPDATE meetings SET state='ended' WHERE source='hosted'")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(HOSTED, (room, *GOOD))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE meetings SET lines_pushed=-1 WHERE source='hosted'")
    other = con.execute(
        "INSERT INTO rooms(name, created_at, created_by, budget_per_hour, budget_remaining,"
        " budget_window_start, hop_limit) VALUES('#two', 1.0, 'alice', 60, 60, 1.0, 30)"
    ).lastrowid
    for n in range(3):  # the broker's own calls have no key, and any number of them may
        con.execute("UPDATE meetings SET state='ended' WHERE room_id=?", (other,))
        con.execute(
            "INSERT INTO meetings(room_id, state, started_at, started_by, platform)"
            " VALUES(?, 'joining', ?, 'a', 'zoom')",
            (other, float(n)),
        )
    con.close()


def test_the_backup_is_the_schema_41_database_and_a_new_one_has_the_same_tables_as_a_migrated_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup written first has the old table (no new column, the old platform CHECK) and every row. A
    database made fresh and one migrated agree on the columns, the indexes and what the platform may be, so
    an upgraded broker and a new one accept the same rows. Fails if only the migration or only the CREATE
    was edited."""
    path = tmp_path / "old.db"
    at_41(path, monkeypatch)
    before = sqlite3.connect(path)
    counts = db.row_counts(before, db.TABLES)
    before.close()
    migrated = db.open_db(path)

    backup = sqlite3.connect(path.with_name("old.db.v41.bak"))
    assert db.schema_version(backup) == 41
    assert (
        "'unknown'" not in backup.execute("SELECT sql FROM sqlite_master WHERE name='meetings'").fetchone()[0]
    )
    assert "source" not in [r[1] for r in backup.execute("PRAGMA table_info(meetings)")]
    assert "meeting_id" not in [r[1] for r in backup.execute("PRAGMA table_info(messages)")]
    assert db.row_counts(backup, db.TABLES) == counts
    backup.close()

    fresh = db.open_db(tmp_path / "new.db")
    for table in ("meetings", "messages"):
        assert [tuple(r) for r in fresh.execute(f"PRAGMA table_info({table})")] == [
            tuple(r) for r in migrated.execute(f"PRAGMA table_info({table})")
        ]
    indexes = (
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name IN ('meetings', 'messages')"
    )
    assert sorted(tuple(r) for r in fresh.execute(indexes)) == sorted(
        tuple(r) for r in migrated.execute(indexes)
    )
    for con in (fresh, migrated):
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name='meetings'").fetchone()[0]
        assert "'zoom','meet','teams','unknown'" in sql
        assert "CHECK(source IN ('server','hosted'))" in sql
    fresh.close()
    migrated.close()

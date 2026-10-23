"""Schema 32 (#498) adds the table that holds the avatars an admin uploads for each harness.

The database is the real 0.37.2 one the schema-30 and schema-31 tests use (``tests/fixtures/db/v0_37_2.sql``,
schema 28), so the step runs after 29, 30 and 31 in one migration, after one verified backup, on rows that
exist. The new table is empty (nobody has uploaded anything), and its columns say what they may hold, in
the schema itself, so a bug in a store function can't write a type, a harness or a size the page would
refuse."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"
INSERT = (
    "INSERT INTO agent_icons(harness, scheme, mime, data, width, height, sha256, updated_at)"
    " VALUES(?,?,?,?,?,?,?,?)"
)


def database_at_28(tmp_path: Path) -> Path:
    path = tmp_path / "switchboard.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    assert db.schema_version(con) == 28
    con.commit()
    con.close()
    os.chmod(path, 0o600)
    return path


def good(**changes: object) -> tuple[object, ...]:
    row: dict[str, object] = {
        "harness": "codex",
        "scheme": "light",
        "mime": "image/png",
        "data": b"\x89PNG",
        "width": 64,
        "height": 64,
        "sha256": hashlib.sha256(b"\x89PNG").hexdigest(),
        "updated_at": 1.0,
    }
    row.update(changes)
    return tuple(row.values())


def test_an_old_database_gains_the_empty_table_and_keeps_every_row(tmp_path: Path) -> None:
    """Every table of a 0.37.2 database has the rows it had, ``agent_icons`` is there and empty, and the
    backup of the old file was made first. Fails if the step isn't in the chain (an old database would
    stay at schema 31 and the broker would refuse it as newer), or if a row is lost."""
    path = database_at_28(tmp_path)
    before = sqlite3.connect(path)
    counts = {t: before.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in db.kept_tables(28)}
    before.close()
    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 34
    assert db.row_counts(con, db.kept_tables(28)) == counts
    assert con.execute("SELECT COUNT(*) FROM agent_icons").fetchone()[0] == 0
    assert "agent_icons" in db.TABLES and "agent_icons" not in db.tables_of(31)
    assert path.with_name(path.name + ".v28.bak").exists()
    con.close()


def test_a_migrated_database_keeps_an_icon_across_a_reopen(tmp_path: Path) -> None:
    """The table the migration makes is the one a new database gets: an icon stored in a migrated file
    is there after the file is closed and opened again, bytes and all."""
    path = database_at_28(tmp_path)
    con = db.open_db(path)
    with db.tx(con):
        con.execute(INSERT, good(data=b"\x89PNG-bytes"))
    con.close()
    con = db.open_db(path)
    row = con.execute("SELECT * FROM agent_icons").fetchone()
    assert (row["harness"], row["scheme"], bytes(row["data"])) == ("codex", "light", b"\x89PNG-bytes")
    con.close()


def test_a_fresh_database_and_a_migrated_one_have_the_same_table(tmp_path: Path) -> None:
    fresh = db.open_db(tmp_path / "fresh.db")
    migrated = db.open_db(database_at_28(tmp_path))

    def shape(con: sqlite3.Connection) -> list[tuple[object, ...]]:
        return [tuple(r) for r in con.execute("PRAGMA table_info(agent_icons)")]

    assert shape(fresh) == shape(migrated) and shape(fresh)

    def text(con: sqlite3.Connection) -> list[str]:  # the CREATE statement, without its layout
        return [
            re.sub(r"\s+", "", r[0])
            for r in con.execute("SELECT sql FROM sqlite_master WHERE name='agent_icons'")
        ]

    assert text(fresh) == text(migrated) and len(text(fresh)) == 1
    fresh.close()
    migrated.close()


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"harness": "test"}, id="harness-test"),  # only the four with an avatar of their own
        pytest.param({"harness": "unknown"}, id="harness-unknown"),
        pytest.param({"scheme": "sepia"}, id="scheme-sepia"),
        pytest.param({"mime": "image/svg+xml"}, id="svg"),  # never SVG, even if a store function forgot
        pytest.param({"mime": "image/gif"}, id="gif"),
        pytest.param({"data": b""}, id="no-bytes"),
        pytest.param({"data": b"x" * (256 * 1024 + 1)}, id="over-256-kb"),
        pytest.param({"width": 0}, id="width-0"),
        pytest.param({"width": 513}, id="width-513"),
        pytest.param({"height": 0}, id="height-0"),
        pytest.param({"height": 513}, id="height-513"),
        pytest.param({"sha256": "short"}, id="short-hash"),
    ],
)
def test_the_columns_say_what_they_may_hold(tmp_path: Path, bad: dict[str, object]) -> None:
    """The harness, the scheme, the type, the size in bytes and in pixels are all checked by the table:
    a row outside them is refused by SQLite itself. Fails if any limit is missing from the schema."""
    con = db.open_db(tmp_path / "t.db")
    with db.tx(con):
        con.execute(INSERT, good())  # the good one goes in
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx(con):
            con.execute(INSERT, good(harness="cursor", **bad) if "harness" not in bad else good(**bad))
    con.close()


def test_one_row_per_harness_and_scheme(tmp_path: Path) -> None:
    con = db.open_db(tmp_path / "t.db")
    with db.tx(con):
        con.execute(INSERT, good())
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx(con):
            con.execute(INSERT, good())
    with db.tx(con):
        con.execute(INSERT, good(scheme="dark"))
        con.execute(INSERT, good(harness="devin"))
    con.close()

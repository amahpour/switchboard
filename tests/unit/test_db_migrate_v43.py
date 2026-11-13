"""Schema 43: a call records when it went live, and an admin can upload the icon of each platform a call is
on (#623, DESIGN.md §29.11, §54).

``meetings`` gains ``live_at``; ``agent_icons`` admits ``zoom``, ``meet`` and ``teams`` beside the five
harnesses, by the same schema-text edit as schema 35's (no table is rebuilt, the step is verified inside
the migration and fails closed). The database is the real 0.37.2 one, migrated to 42 first and given a call
that is live and an avatar in every old slot, so the step runs on rows that exist. The backup written first
is that schema-42 file.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"
HARNESSES = ("claude", "codex", "cursor", "devin", "opencode")
PLATFORMS = ("zoom", "meet", "teams")
ROW = (
    "INSERT INTO agent_icons(harness, scheme, mime, data, width, height, sha256, updated_at)"
    " VALUES(?,?,?,?,?,?,?,?)"
)


def icon_row(name: str, scheme: str = "light", **over: object) -> tuple[object, ...]:
    data = over.pop("data", f"{name}-{scheme}".encode())
    row = {
        "mime": "image/png",
        "width": 64,
        "height": 64,
        "sha256": hashlib.sha256(data).hexdigest(),  # type: ignore[arg-type]
        "updated_at": 1790000000.5,
    }
    row.update(over)
    return (name, scheme, row["mime"], data, row["width"], row["height"], row["sha256"], row["updated_at"])


def at_42(path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """A schema-42 database made from the real old one, with an avatar in every old slot and a call that is
    live (the broker's own). Returns that call's id."""
    old = sqlite3.connect(path)
    old.executescript(FIXTURE.read_text())
    old.commit()
    old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 42)
        con = db.open_db(path)
        assert db.schema_version(con) == 42
        for harness in HARNESSES:
            for scheme in ("light", "dark"):
                con.execute(ROW, icon_row(harness, scheme))
        room = con.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
        con.execute(
            "INSERT INTO meetings(room_id, state, started_at, started_by, platform)"
            " VALUES(?, 'live', 5.0, 'alice', 'zoom')",
            (room,),
        )
        call = con.execute("SELECT id FROM meetings").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):  # before the step, a platform has no icon slot
            con.execute(ROW, icon_row("zoom"))
        assert "live_at" not in [r[1] for r in con.execute("PRAGMA table_info(meetings)")]
        con.close()
    return int(call)


def icons_of(con: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [tuple(r) for r in con.execute("SELECT * FROM agent_icons ORDER BY harness, scheme")]


def table_sql(con: sqlite3.Connection, table: str) -> str:
    return con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]


def squash(sql: str) -> str:
    return " ".join(sql.split()).replace("( ", "(")


def test_every_row_survives_a_platform_icon_can_be_stored_and_an_old_call_has_no_live_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real old database keeps every row of every table, each avatar byte for byte, and its foreign keys.
    The call that was live keeps its state and has no ``live_at`` (nothing recorded when it went live). Zoom,
    Meet and Teams icons can be stored afterwards, a name that is neither a harness nor a platform is still
    refused. The backup written first is the schema-42 database. Fails if the step loses a row, leaves the
    CHECK as it was (a platform refused), widens it to anything else, or gives an old call a time it never
    had."""
    path = tmp_path / "old.db"
    call = at_42(path, monkeypatch)
    before = sqlite3.connect(path)
    counts = db.row_counts(before, db.tables_of(42))
    icons = icons_of(before)
    before.close()
    assert counts["agent_icons"] == 10 and counts["meetings"] == 1

    con = db.open_db(path)  # through to the current version
    assert db.schema_version(con) == db.SCHEMA_VERSION == 50
    assert db.row_counts(con, db.tables_of(42)) == counts
    assert icons_of(con) == icons
    live = con.execute("SELECT state, live_at FROM meetings WHERE id=?", (call,)).fetchone()
    assert tuple(live) == ("live", None)
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    assert db.integrity_ok(con) is None
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0

    for platform in PLATFORMS:
        for scheme in ("light", "dark"):
            con.execute(ROW, icon_row(platform, scheme))
    for bad in ("unknown", "test", "webex", "claude-code", ""):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(ROW, icon_row(bad))
    con.execute("UPDATE meetings SET live_at=9.5 WHERE id=?", (call,))
    con.close()

    again = db.open_db(path)  # reopened: stored, and nothing to migrate
    stored = again.execute(
        "SELECT harness FROM agent_icons WHERE harness IN ('zoom','meet','teams')"
    ).fetchall()
    assert len(stored) == 6
    again.close()

    backup = sqlite3.connect(path.with_name("old.db.v42.bak"))
    assert db.schema_version(backup) == 42
    assert db.row_counts(backup, db.tables_of(42)) == counts
    assert icons_of(backup) == icons
    assert "live_at" not in [r[1] for r in backup.execute("PRAGMA table_info(meetings)")]
    with pytest.raises(sqlite3.IntegrityError):
        backup.execute(ROW, icon_row("zoom"))  # the backup is the old schema, untouched
    backup.close()


def test_every_other_constraint_is_kept_and_a_new_database_is_the_same_as_a_migrated_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the name list changes: a migrated database refuses what a fresh one refuses (a scheme, a type, an
    empty or an oversize file, a side of 0 or 513 pixels, a short hash), and its table text and its meetings
    columns are the fresh schema's, apart from line breaks. Fails if the edit drops or alters any CHECK, or
    if only the migration or only the CREATE was edited."""
    path = tmp_path / "old.db"
    at_42(path, monkeypatch)
    con = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    assert squash(table_sql(con, "agent_icons")) == squash(table_sql(fresh, "agent_icons"))
    assert "'opencode','zoom','meet','teams'" in table_sql(fresh, "agent_icons")
    assert [tuple(r) for r in con.execute("PRAGMA table_info(meetings)")] == [
        tuple(r) for r in fresh.execute("PRAGMA table_info(meetings)")
    ]
    for c in (con, fresh):
        for bad in (
            icon_row("zoom", "sepia"),
            icon_row("zoom", mime="image/svg+xml"),
            icon_row("zoom", mime="image/gif"),
            icon_row("zoom", data=b""),
            icon_row("zoom", data=b"x" * (262144 + 1)),
            icon_row("zoom", width=0),
            icon_row("zoom", height=513),
            icon_row("zoom", sha256="abc"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(ROW, bad)
        c.execute(ROW, icon_row("teams", data=b"x" * 262144))  # the cap itself is fine
        with pytest.raises(sqlite3.IntegrityError):  # a slot holds one image (the primary key)
            c.execute(ROW, icon_row("teams", data=b"y"))
    con.close()
    fresh.close()


def test_unmatched_check_fails_closed_with_v42_and_its_backup_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An equivalent CHECK with different spacing cannot silently skip the name list: the step finds the new
    names missing from the table's text, so the whole migration rolls back. The database stays at schema 42
    with its rows and its table text, without the new column (the ALTER was in the same transaction), schema
    editing is off again, and the backup written first is a good schema-42 copy. Fails if the step reports
    success without the edit, or half applies."""
    path = tmp_path / "old.db"
    at_42(path, monkeypatch)
    con = db.connect(path)
    con.execute("PRAGMA writable_schema=ON")
    con.execute(
        "UPDATE sqlite_master SET sql=replace(sql, '''devin'',''opencode''', '''devin'', ''opencode''')"
        " WHERE type='table' AND name='agent_icons'"
    )
    con.execute("PRAGMA writable_schema=RESET")
    con.close()
    con = db.connect(path)  # reopened, so the edited schema text is what SQLite parses
    before_sql = table_sql(con, "agent_icons")
    assert "'devin', 'opencode'" in before_sql
    counts = db.row_counts(con, db.tables_of(42))
    backup = tmp_path / "before-43.db"
    try:
        with pytest.raises(db.SchemaError, match="agent_icons.*zoom.*unchanged, still schema version 42"):
            db.migrate(con, backup_to=backup)
        assert db.schema_version(con) == 42
        assert table_sql(con, "agent_icons") == before_sql
        assert db.row_counts(con, db.tables_of(42)) == counts
        assert "live_at" not in [r[1] for r in con.execute("PRAGMA table_info(meetings)")]
        assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.integrity_ok(con) is None
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(ROW, icon_row("zoom"))  # still refused: nothing was half done
        with sqlite3.connect(backup) as saved:
            assert db.schema_version(saved) == 42
            assert db.row_counts(saved, db.tables_of(42)) == counts
            assert db.integrity_ok(saved) is None
    finally:
        con.close()


def test_a_failure_in_the_step_rolls_back_and_turns_schema_editing_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A statement that fails after the CHECK was edited restores schema 42, its rows and its table text (the
    edit was inside the transaction), and the normal schema protections. Fails if the edit survives a later
    failure, or writable_schema stays on."""
    path = tmp_path / "old.db"
    at_42(path, monkeypatch)
    con = db.connect(path)
    counts = db.row_counts(con, db.tables_of(42))
    before_sql = table_sql(con, "agent_icons")
    monkeypatch.setattr(db, "V42_TO_V43", (*db.V42_TO_V43, "SELECT missing_column FROM agent_icons"))
    try:
        with pytest.raises(db.SchemaError, match="unchanged, still schema version 42"):
            db.migrate(con, backup_to=tmp_path / "before-43.db")
        assert db.schema_version(con) == 42
        assert table_sql(con, "agent_icons") == before_sql
        assert db.row_counts(con, db.tables_of(42)) == counts
        assert "live_at" not in [r[1] for r in con.execute("PRAGMA table_info(meetings)")]
        assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.integrity_ok(con) is None
    finally:
        con.close()

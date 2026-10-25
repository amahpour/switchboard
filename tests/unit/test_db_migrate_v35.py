"""Schema 35 admits OpenCode in the agent_icons CHECK without rebuilding the table (§29.11, #518).

The database is the real 0.37.2 one the schema-30 to 34 tests use (``tests/fixtures/db/v0_37_2.sql``, schema
28), migrated to 34 first and given the avatars an admin may have uploaded for the four harnesses that were
allowed then, so the step runs on rows that exist. The step is the same schema-text edit as 34's: it is
verified inside the migration and fails closed."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"
OLD = ("claude", "codex", "cursor", "devin")
ROW = (
    "INSERT INTO agent_icons(harness, scheme, mime, data, width, height, sha256, updated_at)"
    " VALUES(?,?,?,?,?,?,?,?)"
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def icon_row(harness: str, scheme: str = "light", **over: object) -> tuple[object, ...]:
    data = over.pop("data", f"{harness}-{scheme}".encode())
    row = {
        "mime": "image/png",
        "width": 64,
        "height": 64,
        "sha256": sha(data),  # type: ignore[arg-type]
        "updated_at": 1790000000.5,
    }
    row.update(over)
    return (harness, scheme, row["mime"], data, row["width"], row["height"], row["sha256"], row["updated_at"])


def at_34(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A schema-34 database made from the real old one, with an uploaded avatar in every old slot."""
    old = sqlite3.connect(path)
    old.executescript(FIXTURE.read_text())
    old.commit()
    old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 34)
        con = db.open_db(path)
        assert db.schema_version(con) == 34
        for harness in OLD:
            for scheme in ("light", "dark"):
                con.execute(ROW, icon_row(harness, scheme))
        con.close()


def icons_of(con: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [tuple(r) for r in con.execute("SELECT * FROM agent_icons ORDER BY harness, scheme")]


def table_sql(con: sqlite3.Connection, table: str) -> str:
    return con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]


def squash(sql: str) -> str:
    return " ".join(sql.split()).replace("( ", "(")


def test_old_icons_and_every_other_row_survive_and_opencode_can_be_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real old database keeps every row of every table, each uploaded avatar byte for byte, and its
    foreign keys; OpenCode's avatar can be stored afterwards, and a harness that is not one is still
    refused. The backup written first is the schema-34 database. Fails if the step loses a row, leaves
    the CHECK as it was (OpenCode refused), or widens it to anything else."""
    path = tmp_path / "old.db"
    at_34(path, monkeypatch)
    before = sqlite3.connect(path)
    counts = db.row_counts(before, db.TABLES)
    icons = icons_of(before)
    before.close()
    assert counts["agent_icons"] == 8 and counts["participants"] > 0

    con = db.open_db(path)  # the current version: 34 -> 35
    assert db.schema_version(con) == db.SCHEMA_VERSION == 35
    assert db.row_counts(con, db.TABLES) == counts
    assert icons_of(con) == icons
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    assert db.integrity_ok(con) is None
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0

    con.execute(ROW, icon_row("opencode", "light"))
    con.execute(ROW, icon_row("opencode", "dark"))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ROW, icon_row("not-a-harness"))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ROW, icon_row("test"))  # the other harnesses participants allow are still not avatars
    con.close()

    again = db.open_db(path)  # reopened: stored, and nothing to migrate
    assert again.execute("SELECT count(*) FROM agent_icons WHERE harness='opencode'").fetchone()[0] == 2
    again.close()
    backup = sqlite3.connect(path.with_name("old.db.v34.bak"))
    assert db.schema_version(backup) == 34
    assert db.row_counts(backup, db.TABLES) == counts
    assert icons_of(backup) == icons
    with pytest.raises(sqlite3.IntegrityError):
        backup.execute(ROW, icon_row("opencode"))  # the backup is the old schema, untouched
    backup.close()


def test_every_other_constraint_of_the_table_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the harness list changes: a migrated database refuses what a fresh one refuses (a scheme, a
    type, an empty or an oversize file, a side of 0 or 513 pixels, a short hash, a second row for a slot)
    and its table text is the fresh schema's, apart from line breaks. Fails if the edit drops or alters
    any CHECK, the primary key, or leaves the table different from what a new database has."""
    path = tmp_path / "old.db"
    at_34(path, monkeypatch)
    con = db.open_db(path)
    fresh = db.open_db(tmp_path / "fresh.db")
    # a migrated table was made by 32's statement, a fresh one by the schema script: same text but for layout
    assert squash(table_sql(con, "agent_icons")) == squash(table_sql(fresh, "agent_icons"))
    assert "'devin','opencode'" in table_sql(fresh, "agent_icons")
    for c in (con, fresh):
        for bad in (
            icon_row("opencode", "sepia"),
            icon_row("opencode", mime="image/svg+xml"),
            icon_row("opencode", mime="image/gif"),
            icon_row("opencode", data=b""),
            icon_row("opencode", data=b"x" * (262144 + 1)),
            icon_row("opencode", width=0),
            icon_row("opencode", height=513),
            icon_row("opencode", sha256="abc"),
            icon_row("claude", "light"),  # the slot is taken (PRIMARY KEY) on the migrated one
        ):
            if c is fresh and bad[0] == "claude":
                continue  # nothing is in the fresh database yet
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(ROW, bad)
        c.execute(ROW, icon_row("opencode", data=b"x" * 262144))  # the cap itself is fine
    con.close()
    fresh.close()


def test_unmatched_check_fails_closed_with_v34_and_its_backup_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An equivalent CHECK with different spacing cannot silently skip the harness update: the step finds
    the new harness missing from the table's text, so the whole migration rolls back. The database stays
    at schema 34 with its rows and its table text, schema editing is off again, and the backup written
    first is a good schema-34 copy. Fails if the step reports success without the edit, or half applies."""
    path = tmp_path / "old.db"
    at_34(path, monkeypatch)
    con = db.connect(path)
    con.execute("PRAGMA writable_schema=ON")
    con.execute(
        "UPDATE sqlite_master SET sql=replace(sql, '''claude'',''codex''', '''claude'', ''codex''')"
        " WHERE type='table' AND name='agent_icons'"
    )
    con.execute("PRAGMA writable_schema=RESET")
    con.close()
    con = db.connect(path)  # reopened, so the edited schema text is what SQLite parses
    before_sql = table_sql(con, "agent_icons")
    assert "'claude', 'codex'" in before_sql
    counts = db.row_counts(con, db.TABLES)
    backup = tmp_path / "before-35.db"
    try:
        with pytest.raises(db.SchemaError, match="agent_icons.*opencode.*unchanged, still schema version 34"):
            db.migrate(con, backup_to=backup)
        assert db.schema_version(con) == 34
        assert table_sql(con, "agent_icons") == before_sql
        assert db.row_counts(con, db.TABLES) == counts
        assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.integrity_ok(con) is None
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(ROW, icon_row("opencode"))  # still refused: nothing was half done
        with sqlite3.connect(backup) as saved:
            assert db.schema_version(saved) == 34
            assert db.row_counts(saved, db.TABLES) == counts
            assert table_sql(saved, "agent_icons") == before_sql
            assert db.integrity_ok(saved) is None
    finally:
        con.close()


def test_a_failure_in_the_step_rolls_back_and_turns_schema_editing_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A statement that fails after the CHECK was edited restores schema 34, its rows and its table text
    (the edit was inside the transaction), and the normal schema protections. Fails if the edit survives
    a later failure, or writable_schema stays on."""
    path = tmp_path / "old.db"
    at_34(path, monkeypatch)
    con = db.connect(path)
    counts = db.row_counts(con, db.TABLES)
    before_sql = table_sql(con, "agent_icons")
    monkeypatch.setattr(db, "V34_TO_V35", (*db.V34_TO_V35, "SELECT missing_column FROM agent_icons"))
    try:
        with pytest.raises(db.SchemaError, match="unchanged, still schema version 34"):
            db.migrate(con, backup_to=tmp_path / "before-35.db")
        assert db.schema_version(con) == 34
        assert table_sql(con, "agent_icons") == before_sql
        assert db.row_counts(con, db.TABLES) == counts
        assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.integrity_ok(con) is None
    finally:
        con.close()

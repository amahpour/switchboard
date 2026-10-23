"""Schema 30 (#385) ends the sessions of the local agent path and drops the columns only that path used.

The database is a real one: ``tests/fixtures/db/v0_37_2.sql`` was written by switchboard 0.37.2, the
last release with a per-session MCP server and a hook script (``make_v0_37_2.py`` made it), and has
sessions of the local path (a harness on this machine), of an SSH remote, of a machine that dialed in,
and over HTTPS. It is schema 28, so it takes both retiring steps (29 and 30) in one go, after one
verified backup."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from switchboard import db
from switchboard.store import Store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"
# what only the local path used (the columns schema 30 drops)
DROPPED_PARTICIPANT_COLUMNS = {
    "agent_pid",
    "agent_start",
    "mcp_pid",
    "mcp_start",
    "claude_socket",
    "bind_state",
    "bind_nonce",
    "thread_proof",
    "env_leak",
    "last_loop_count",
    "unconfirmed_followups",
    "push_expiries",
}


def database_at_28(tmp_path: Path) -> Path:
    path = tmp_path / "switchboard.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    assert db.schema_version(con) == 28
    con.commit()
    con.close()
    os.chmod(path, 0o600)
    return path


def is_https(row: sqlite3.Row) -> bool:
    """A session over HTTPS: a device host and no MCP pid (§49.3)."""
    return row["host"] != "" and row["mcp_pid"] == 0


def test_the_0_37_2_database_migrates_keeping_every_row_and_ending_every_local_session(
    tmp_path: Path,
) -> None:
    """Rooms, messages, people, passkeys, web sessions, agent tokens, claims, pins, the board and every
    other row of a 0.37.2 database are there after the migration. Every session that reached the broker
    another way than HTTPS ends, leaving its rooms as a session end does (reason ``retired``, its open
    deliveries revoked, its offers cancelled); a session over HTTPS is as it was, in every column that
    stays. Fails if a row is lost, a session of the local path is left as a ghost, an agent that can
    still be reached is ended, or a column that should go stays."""
    path = database_at_28(tmp_path)
    old = db.connect(path)
    before = db.row_counts(old, db.tables_of(28))
    old_rows = {r["session_key"]: dict(r) for r in old.execute("SELECT * FROM participants")}
    https_before = {k for k, r in old_rows.items() if is_https(r)}
    local_before = {k for k, r in old_rows.items() if not is_https(r) and r["ended_at"] is None}
    assert https_before and local_before
    assert {r["host"] for r in old_rows.values() if not is_https(r)} >= {"", "fpga-pi", "work-laptop"}
    open_https = old.execute(
        "SELECT COUNT(*) FROM memberships m JOIN participants p ON p.id=m.participant_id"
        " WHERE m.left_at IS NULL AND p.host != '' AND p.mcp_pid = 0"
    ).fetchone()[0]
    assert open_https > 0
    old.close()

    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 34
    assert db.integrity_ok(con) is None
    assert db.row_counts(con, db.kept_tables(28)) == {t: before[t] for t in db.kept_tables(28)}
    assert path.with_name(path.name + ".v28.bak").exists()
    assert not list(tmp_path.glob("switchboard.db.v29.bak*"))  # one backup, of the file as it was

    for key in local_before:
        now = con.execute("SELECT * FROM participants WHERE session_key=?", (key,)).fetchone()
        assert now["ended_at"] is not None and now["status"] == "offline" and now["status_src"] == "retired"
        left = con.execute(
            "SELECT left_at, left_reason FROM memberships WHERE participant_id=?", (now["id"],)
        ).fetchall()
        assert left and all(m["left_at"] is not None for m in left), key
        assert not con.execute(
            "SELECT 1 FROM deliveries d JOIN memberships m ON m.id=d.membership_id"
            " WHERE m.participant_id=? AND d.state IN ('pending','offered','in_context')",
            (now["id"],),
        ).fetchone()
        assert not con.execute(
            "SELECT 1 FROM batches b JOIN memberships m ON m.id=b.membership_id"
            " WHERE m.participant_id=? AND b.state='offered'",
            (now["id"],),
        ).fetchone()

    # the sessions over HTTPS keep every membership, and every column that stays
    open_after = con.execute(
        "SELECT COUNT(*) FROM memberships m JOIN participants p ON p.id=m.participant_id"
        " WHERE m.left_at IS NULL AND p.host != ''"
    ).fetchone()[0]
    assert open_after == open_https
    for key in https_before:
        now = dict(con.execute("SELECT * FROM participants WHERE session_key=?", (key,)).fetchone())
        assert now == {k: v for k, v in old_rows[key].items() if k not in DROPPED_PARTICIPANT_COLUMNS}, key
    con.close()


def test_the_columns_and_the_index_only_the_local_path_used_are_gone(tmp_path: Path) -> None:
    """A migrated database has the fresh schema's tables, column by column and index by index. Fails if a
    column the local path used is left (so a store written for HTTPS only would sit beside dead data), or
    a column or index differs from a fresh database's."""
    migrated = db.open_db(database_at_28(tmp_path))
    fresh = db.open_db(tmp_path / "fresh.db")
    cols = lambda c, t: [tuple(r) for r in c.execute(f"PRAGMA table_info({t})")]  # noqa: E731
    assert not DROPPED_PARTICIPANT_COLUMNS & {c[1] for c in cols(migrated, "participants")}
    assert "cred_hash" not in {c[1] for c in cols(migrated, "memberships")}
    for table in db.TABLES:
        assert cols(migrated, table) == cols(fresh, table), table
    q = "SELECT name, tbl_name FROM sqlite_master WHERE type='index' ORDER BY name"
    assert migrated.execute(q).fetchall() == fresh.execute(q).fetchall()
    assert not migrated.execute("SELECT 1 FROM sqlite_master WHERE name='participants_host_mcp'").fetchone()
    migrated.close()
    fresh.close()


def test_the_store_works_on_a_migrated_database(tmp_path: Path) -> None:
    """What 0.37.2 wrote is readable by this release's store: the rooms, their messages, and only the
    sessions over HTTPS as active members."""
    con = db.open_db(database_at_28(tmp_path))
    store = Store(con)
    rooms = store.list_rooms()
    assert rooms
    active = [m for r in rooms for m in store.members(r.id)]
    assert active and all(m.host != "" for m in active)
    assert all(store.history(r.id) for r in rooms[:1])
    con.close()


def test_a_failed_step_leaves_the_database_as_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both steps run in the migration's one transaction: if a statement of the last fails, nothing of
    either is kept: the retired tables are still there, no session was ended and no column was dropped.
    Fails if the updates or a drop escape the rollback."""
    path = database_at_28(tmp_path)
    con = db.connect(path)
    ended_before = con.execute("SELECT COUNT(*) FROM participants WHERE ended_at IS NOT NULL").fetchone()[0]
    con.close()
    steps = db._steps

    def broken(frm: int):  # noqa: ANN202
        out = steps(frm)
        to, stmts, new = out[-1]
        return [*out[:-1], (to, (*stmts[:-1], "DROP TABLE no_such_table", stmts[-1]), new)]

    monkeypatch.setattr(db, "_steps", broken)
    with pytest.raises(db.SchemaError):
        db.open_db(path)
    con = db.connect(path)
    assert db.schema_version(con) == 28
    assert con.execute("SELECT COUNT(*) FROM remotes").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM participants WHERE ended_at IS NOT NULL").fetchone()[0] == (
        ended_before
    )
    assert DROPPED_PARTICIPANT_COLUMNS <= {r[1] for r in con.execute("PRAGMA table_info(participants)")}
    con.close()

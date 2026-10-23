"""Schema 29 (#385) drops the tables of SSH remotes and of machines that dial in, ends the sessions that
only they could reach, and keeps everything else. The next step (30) has its own file; this one stops
at 29, so the step is seen on its own.

The database is a real one: ``tests/fixtures/db/v0_37_2.sql`` was written by switchboard 0.37.2, the
last release with both ways for an agent to reach the broker (``make_v0_37_2.py`` made it), and has
agents of the local path, of an SSH remote, of a machine that dials in and over HTTPS."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_37_2.sql"
# the sessions of a host that no longer exists: a host name and a real MCP pid
RETIRED_HOSTS = ("fpga-pi", "work-laptop")


@pytest.fixture(autouse=True)
def stop_at_29(monkeypatch: pytest.MonkeyPatch) -> None:
    """The step to 30 ends the local sessions too and drops columns; this file is about 29 alone."""
    monkeypatch.setattr(db, "SCHEMA_VERSION", 29)


def database_at_28(tmp_path: Path) -> Path:
    path = tmp_path / "switchboard.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    assert db.schema_version(con) == 28
    con.commit()
    con.close()
    os.chmod(path, 0o600)
    return path


def sessions(con: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["session_key"].split(":")[0]: r for r in con.execute("SELECT * FROM participants")}


def test_the_0_37_2_database_migrates_keeping_every_row_and_ending_only_the_retired_sessions(
    tmp_path: Path,
) -> None:
    """Rooms, messages, people, passkeys, web sessions, agent tokens, claims, pins, the board and every
    other row of a 0.37.2 database are all there after the migration; the two retired tables are gone;
    only the sessions of the SSH remote and of the machine that dialed in end, leaving their rooms as
    a session end does; a session over HTTPS and a local one are as they were. Fails if a table is
    kept that should go, a row is lost, a retired host's agent is left as a ghost, or an agent that
    can still be reached is ended."""
    path = database_at_28(tmp_path)
    old = db.connect(path)
    before = db.row_counts(old, db.tables_of(28))
    old_rows = {r["session_key"]: dict(r) for r in old.execute("SELECT * FROM participants")}
    open_before = {
        r[0]: r[1]
        for r in old.execute(
            "SELECT p.host, COUNT(*) FROM memberships m JOIN participants p ON p.id=m.participant_id"
            " WHERE m.left_at IS NULL GROUP BY p.host"
        )
    }
    assert {"fpga-pi", "work-laptop", "laptop", "desktop", ""} <= set(open_before)
    old.close()

    con = db.open_db(path)
    assert db.schema_version(con) == db.SCHEMA_VERSION
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {"remotes", "link_machines"} & tables
    assert db.row_counts(con, db.kept_tables(28)) == {t: before[t] for t in db.kept_tables(28)}
    assert before["remotes"] == 1 and before["link_machines"] == 3  # what the backup still holds
    assert before["messages"] >= 20 and before["people"] == 1 and before["agent_tokens"] == 3
    assert path.with_name(path.name + ".v28.bak").exists()

    for key, row in ((k, r) for k, r in old_rows.items() if r["host"] in RETIRED_HOSTS):
        now = con.execute("SELECT * FROM participants WHERE session_key=?", (key,)).fetchone()
        assert now["ended_at"] is not None and now["status"] == "offline" and now["status_src"] == "retired"
        assert row["ended_at"] is None
        left = con.execute(
            "SELECT left_at, left_reason FROM memberships WHERE participant_id=?", (now["id"],)
        ).fetchall()
        assert left and all(m["left_at"] is not None and m["left_reason"] == "retired" for m in left), key
        assert not con.execute(
            "SELECT 1 FROM deliveries d JOIN memberships m ON m.id=d.membership_id"
            " WHERE m.participant_id=? AND d.state IN ('pending','offered','in_context')",
            (now["id"],),
        ).fetchone()

    open_after = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT p.host, COUNT(*) FROM memberships m JOIN participants p ON p.id=m.participant_id"
            " WHERE m.left_at IS NULL GROUP BY p.host"
        )
    }
    # the sessions over HTTPS (a device host, MCP pid 0) and the local ones keep every membership
    assert open_after == {h: n for h, n in open_before.items() if h not in RETIRED_HOSTS}
    for key, row in ((k, r) for k, r in old_rows.items() if r["host"] not in RETIRED_HOSTS):
        now = dict(con.execute("SELECT * FROM participants WHERE session_key=?", (key,)).fetchone())
        assert now == row, key
    assert {r["host"] for r in old_rows.values() if r["mcp_pid"] == 0} == {"laptop", "desktop"}
    con.close()


def test_a_failed_step_leaves_the_database_as_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The step runs in the migration's one transaction: if a statement fails, the retired tables are
    still there and no session was ended. Fails if the drop or the updates escape the rollback."""
    path = database_at_28(tmp_path)
    con = db.connect(path)
    ended_before = con.execute("SELECT COUNT(*) FROM participants WHERE ended_at IS NOT NULL").fetchone()[0]
    con.close()
    steps = db._steps

    def broken(frm: int):  # noqa: ANN202
        out = steps(frm)
        to, stmts, new, edits = out[-1]
        return [*out[:-1], (to, (*stmts[:-1], "DROP TABLE no_such_table", stmts[-1]), new, edits)]

    monkeypatch.setattr(db, "_steps", broken)
    with pytest.raises(db.SchemaError):
        db.open_db(path)
    con = db.connect(path)
    assert db.schema_version(con) == 28
    assert con.execute("SELECT COUNT(*) FROM remotes").fetchone()[0] == 1
    now = con.execute("SELECT COUNT(*) FROM participants WHERE ended_at IS NOT NULL").fetchone()[0]
    assert now == ended_before
    con.close()

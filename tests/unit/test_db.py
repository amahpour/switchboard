"""db: WAL, BEGIN IMMEDIATE, the full schema, crash reopen (DESIGN.md §4, §12.1)."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from switchboard import db

EXPECTED_TABLES = set(db.TABLES)


def test_open_creates_full_schema_with_wal(tmp_path: Path) -> None:
    con = db.open_db(tmp_path / "y.db")
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert con.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert con.isolation_level is None
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= tables
    assert db.schema_version(con) == db.SCHEMA_VERSION == 1
    indexes = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"memberships_active_name", "memberships_active_part", "messages_room_id",
            "deliveries_open", "events_kind_ts"} <= indexes
    assert (os.stat(tmp_path / "y.db").st_mode & 0o777) == 0o600


def test_schema_columns_match_design(tmp_path: Path) -> None:
    con = db.open_db(tmp_path / "y.db")

    def cols(t: str) -> set[str]:
        return {r[1] for r in con.execute(f"PRAGMA table_info({t})")}

    assert {"budget_notice_window", "hop_limit", "last_msg_at", "paused_reason"} <= cols("rooms")
    assert {"bind_nonce", "thread_proof", "approval_mode", "env_leak", "boundary_seq", "gen_tainted",
            "rearms_in_gen", "unconfirmed_followups", "push_expiries", "hooks_seen_at"} <= cols("participants")
    assert {"cred_hash", "join_msg_id", "kicked", "held", "cursor_id", "peer_batch_boundary"} <= cols("memberships")
    assert {"sender_membership_id", "sender_harness", "via", "kind", "mentions", "reply_to"} <= cols("messages")
    assert {"wake_kind", "wake_reason", "budget_counted", "turn_start_at", "first_action_at", "evidence"} <= cols("batches")
    assert {"prio", "mentioned", "offered_inline", "notified_at", "redelivered", "reminders"} <= cols("deliveries")
    assert {"id_hash", "expires_at"} <= cols("web_sessions")


def test_migrate_is_idempotent_and_refuses_unknown_version(tmp_path: Path) -> None:
    p = tmp_path / "y.db"
    con = db.open_db(p)
    assert db.migrate(con) == 1
    con.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(db.SchemaError):
        db.migrate(con)


def test_check_constraints(tmp_path: Path) -> None:
    con = db.open_db(tmp_path / "y.db")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO participants(harness, session_key, created_at) VALUES('bogus','k',0)"
        )
    con.execute("INSERT INTO rooms(name, created_at, created_by, budget_per_hour, budget_remaining,"
                " budget_window_start, hop_limit) VALUES('#a',0,'alice',60,60,0,6)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO messages(room_id, ts, sender_name, sender_kind, via, text)"
                    " VALUES(1, 0, 'x', 'robot', 'web', 't')")


def test_tx_rolls_back_and_nests(tmp_path: Path) -> None:
    con = db.open_db(tmp_path / "y.db")
    con.execute("CREATE TABLE t(x INTEGER)")
    with pytest.raises(RuntimeError):
        with db.tx(con):
            con.execute("INSERT INTO t VALUES(1)")
            raise RuntimeError("boom")
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    with db.tx(con):
        con.execute("INSERT INTO t VALUES(1)")
        with db.tx(con):  # joins the outer transaction
            con.execute("INSERT INTO t VALUES(2)")
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    assert not con.in_transaction


def test_no_lost_updates_under_begin_immediate(tmp_path: Path) -> None:
    """4 threads x 500 read-modify-writes, each on its own connection: exact total."""
    p = tmp_path / "y.db"
    con = db.open_db(p)
    con.execute("CREATE TABLE counter(id INTEGER PRIMARY KEY, n INTEGER NOT NULL)")
    con.execute("INSERT INTO counter VALUES(1, 0)")
    errors: list[BaseException] = []

    def worker() -> None:
        c = db.connect(p)
        try:
            for _ in range(500):
                with db.tx(c):
                    n = c.execute("SELECT n FROM counter WHERE id=1").fetchone()[0]
                    c.execute("UPDATE counter SET n=? WHERE id=1", (n + 1,))
        except BaseException as e:  # pragma: no cover
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    assert con.execute("SELECT n FROM counter WHERE id=1").fetchone()[0] == 2000


def test_reopen_after_crash(tmp_path: Path) -> None:
    """A writer killed with SIGKILL mid-stream: committed rows survive, db is intact."""
    p = tmp_path / "y.db"
    db.open_db(p).close()
    src = Path(__file__).resolve().parents[2] / "src"
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(src)!r})
        from switchboard import db
        con = db.connect({str(p)!r})
        con.execute("CREATE TABLE IF NOT EXISTS crash(i INTEGER)")
        i = 0
        while True:
            with db.tx(con):
                con.execute("INSERT INTO crash VALUES(?)", (i,))
            i += 1
            if i == 200:
                print("ready", flush=True)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    time.sleep(0.05)
    proc.send_signal(signal.SIGKILL)
    proc.wait(5)
    con = db.open_db(p)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    n = con.execute("SELECT COUNT(*) FROM crash").fetchone()[0]
    assert n >= 200
    # rows are a contiguous prefix: no torn transaction
    assert con.execute("SELECT MAX(i) FROM crash").fetchone()[0] == n - 1


def test_readonly_connection_cannot_write(tmp_path: Path) -> None:
    p = tmp_path / "y.db"
    db.open_db(p).close()
    ro = db.connect(p, readonly=True)
    assert ro.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO meta VALUES('x','y')")

"""SQLite connection, transactions and the full schema (DESIGN.md §4).

Every write path runs inside ``with tx(con):`` which issues ``BEGIN IMMEDIATE``.
Connections use ``isolation_level=None``: Python's default transaction mode
silently loses updates on read-then-write (FINDINGS §10 S5).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = r"""
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE rooms(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL, created_by TEXT NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0, paused_reason TEXT,
  budget_per_hour INTEGER NOT NULL, budget_remaining INTEGER NOT NULL, budget_window_start REAL NOT NULL,
  budget_notice_window REAL,
  hop_count INTEGER NOT NULL DEFAULT 0, hop_limit INTEGER NOT NULL,
  last_msg_at REAL);

CREATE TABLE participants(
  id INTEGER PRIMARY KEY,
  harness TEXT NOT NULL CHECK(harness IN ('claude','codex','cursor','devin','test','unknown')),
  session_key TEXT NOT NULL,
  session_id TEXT,
  agent_pid INTEGER, agent_start REAL,
  mcp_pid INTEGER, mcp_start REAL,
  claude_socket TEXT,
  bind_state TEXT NOT NULL DEFAULT 'bound' CHECK(bind_state IN ('bound','pending')),
  bind_nonce TEXT,
  thread_proof INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'starting'
     CHECK(status IN ('starting','idle','busy','waiting-approval','offline')),
  status_at REAL, status_src TEXT,
  tier TEXT,
  tier_note TEXT,
  approval_mode TEXT NOT NULL DEFAULT 'unknown' CHECK(approval_mode IN ('bypass','prompting','unknown')),
  env_leak INTEGER NOT NULL DEFAULT 0,
  away TEXT,
  boundary_seq INTEGER NOT NULL DEFAULT 0,
  gen TEXT, gen_tainted INTEGER NOT NULL DEFAULT 0, rearms_in_gen INTEGER NOT NULL DEFAULT 0,
  last_loop_count INTEGER, unconfirmed_followups INTEGER NOT NULL DEFAULT 0,
  push_expiries INTEGER NOT NULL DEFAULT 0,
  hooks_seen_at REAL, last_say_at REAL, created_at REAL NOT NULL, last_seen REAL, ended_at REAL,
  UNIQUE(harness, session_key));

CREATE TABLE memberships(
  id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
  participant_id INTEGER NOT NULL REFERENCES participants(id),
  screen_name TEXT NOT NULL COLLATE NOCASE,
  cred_hash TEXT,
  joined_at REAL NOT NULL, join_msg_id INTEGER NOT NULL,
  left_at REAL, left_reason TEXT,
  kicked INTEGER NOT NULL DEFAULT 0,
  held INTEGER NOT NULL DEFAULT 0, held_at REAL,
  cursor_id INTEGER NOT NULL DEFAULT 0,
  peer_batch_boundary INTEGER NOT NULL DEFAULT -1);
CREATE UNIQUE INDEX memberships_active_name ON memberships(room_id, screen_name) WHERE left_at IS NULL;
CREATE UNIQUE INDEX memberships_active_part ON memberships(room_id, participant_id) WHERE left_at IS NULL;

CREATE TABLE messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL REFERENCES rooms(id), ts REAL NOT NULL,
  sender_membership_id INTEGER,
  sender_name TEXT NOT NULL, sender_harness TEXT,
  sender_kind TEXT NOT NULL CHECK(sender_kind IN ('human','agent','system')),
  via TEXT NOT NULL CHECK(via IN ('web','cli','mcp','system')),
  kind TEXT NOT NULL DEFAULT 'chat' CHECK(kind IN ('chat','join','leave','notice')),
  text TEXT NOT NULL, reply_to INTEGER, mentions TEXT NOT NULL DEFAULT '[]');
CREATE INDEX messages_room_id ON messages(room_id, id);

CREATE TABLE batches(
  id INTEGER PRIMARY KEY AUTOINCREMENT, membership_id INTEGER NOT NULL REFERENCES memberships(id),
  path TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('priority','wake','pull')),
  wake_kind TEXT,
  wake_reason TEXT,
  budget_counted INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'offered' CHECK(state IN ('offered','confirmed','expired','cancelled')),
  created_at REAL NOT NULL, posted_at REAL, confirmed_at REAL, expired_at REAL, expire_reason TEXT,
  turn_start_at REAL, first_action_at REAL, evidence TEXT);

CREATE TABLE deliveries(
  membership_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
  prio INTEGER NOT NULL,
  mentioned INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'pending'
     CHECK(state IN ('pending','offered','in_context','handled','revoked')),
  batch_id INTEGER, offered_inline INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  notified_at REAL,
  in_context_at REAL, handled_at REAL,
  redelivered INTEGER NOT NULL DEFAULT 0, reminders INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(membership_id, message_id)) WITHOUT ROWID;
CREATE INDEX deliveries_open ON deliveries(membership_id, state, message_id);

CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, room_id INTEGER,
  membership_id INTEGER, participant_id INTEGER, kind TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}');
CREATE INDEX events_kind_ts ON events(kind, ts);

CREATE TABLE web_sessions(id_hash TEXT PRIMARY KEY, created_at REAL NOT NULL,
  last_seen REAL NOT NULL, expires_at REAL NOT NULL);
"""

TABLES = (
    "meta",
    "rooms",
    "participants",
    "memberships",
    "messages",
    "batches",
    "deliveries",
    "events",
    "web_sessions",
)


class SchemaError(RuntimeError):
    pass


def _statements(script: str) -> list[str]:
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise SchemaError("incomplete statement at end of schema")
    return out


SCHEMA_STATEMENTS = _statements(SCHEMA)


def connect(path: str | os.PathLike, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the database with WAL, NORMAL sync, foreign keys and a 5 s busy timeout."""
    if readonly:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(
            uri, uri=True, timeout=5.0, isolation_level=None, check_same_thread=False
        )
    else:
        con = sqlite3.connect(
            str(path), timeout=5.0, isolation_level=None, check_same_thread=False
        )
    con.row_factory = sqlite3.Row
    if not readonly:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con


@contextmanager
def tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE … COMMIT``; rolls back on any exception.

    Nested use joins the outer transaction, so service code can compose
    several store writes into one atomic unit.
    """
    if con.in_transaction:
        yield con
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def schema_version(con: sqlite3.Connection) -> int | None:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if row is None:
        return None
    v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(v[0]) if v else None


def migrate(con: sqlite3.Connection) -> int:
    """Create the full schema on an empty database; refuse unknown versions."""
    with tx(con):
        v = schema_version(con)
        if v is None:
            for stmt in SCHEMA_STATEMENTS:
                con.execute(stmt)
            con.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            return SCHEMA_VERSION
    if v != SCHEMA_VERSION:
        raise SchemaError(
            f"database schema version {v} is not supported (expected {SCHEMA_VERSION})"
        )
    return v


def open_db(path: str | os.PathLike) -> sqlite3.Connection:
    """connect() + migrate(), creating the file 0600."""
    p = Path(path)
    if not p.exists():
        fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
    con = connect(p)
    migrate(con)
    return con


def checkpoint(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA wal_checkpoint(PASSIVE)")

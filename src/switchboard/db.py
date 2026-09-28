"""SQLite connection, transactions, the full schema and its migrations (DESIGN.md §4, §27.6).

Every write path runs inside ``with tx(con):`` which issues ``BEGIN IMMEDIATE``.
Connections use ``isolation_level=None``: Python's default transaction mode
silently loses updates on read-then-write (FINDINGS §10 S5).

Schema versions: 1 (0.1.0 and 0.2.0) and 2 (remote members: ``participants.host``,
``messages.sender_host``, the ``remotes`` table). A version-1 database is migrated
once, after a verified 0600 backup (``migrate``); a newer one is refused.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("switchboard.db")

SCHEMA_VERSION = 2

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
  host TEXT NOT NULL DEFAULT '',
  UNIQUE(harness, session_key));
CREATE INDEX participants_host_mcp ON participants(host, mcp_pid) WHERE ended_at IS NULL;

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
  text TEXT NOT NULL, reply_to INTEGER, mentions TEXT NOT NULL DEFAULT '[]',
  sender_host TEXT);
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

CREATE TABLE remotes(
  name TEXT PRIMARY KEY, config_hash TEXT NOT NULL,
  enabled_at REAL, enabled_via TEXT CHECK(enabled_via IN ('cli','web')),
  blocked_at REAL, blocked_reason TEXT, last_up_at REAL);
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
    "remotes",
)
# the tables of a version-1 database (0.1.0, 0.2.0): the rows a migration must keep
V1_TABLES = TABLES[:-1]

# v1 -> v2 (DESIGN.md §27.6): one BEGIN IMMEDIATE, after a verified backup. The
# columns land at the end of their tables, where the fresh schema above puts them too.
V1_TO_V2 = (
    "ALTER TABLE participants ADD COLUMN host TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE messages ADD COLUMN sender_host TEXT",
    "CREATE INDEX participants_host_mcp ON participants(host, mcp_pid) WHERE ended_at IS NULL",
    "CREATE TABLE remotes("
    " name TEXT PRIMARY KEY, config_hash TEXT NOT NULL,"
    " enabled_at REAL, enabled_via TEXT CHECK(enabled_via IN ('cli','web')),"
    " blocked_at REAL, blocked_reason TEXT, last_up_at REAL)",
    "UPDATE meta SET value='2' WHERE key='schema_version'",
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


def row_counts(con: sqlite3.Connection, tables: tuple[str, ...] = V1_TABLES) -> dict[str, int]:
    """``COUNT(*)`` of each table (names come from this module, never from input)."""
    return {t: int(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in tables}


def integrity_ok(con: sqlite3.Connection) -> str | None:
    """None if ``PRAGMA integrity_check`` says ok, else its first lines."""
    rows = [str(r[0]) for r in con.execute("PRAGMA integrity_check").fetchall()]
    return None if rows == ["ok"] else "; ".join(rows[:5])


def _create_backup_file(path: Path) -> Path:
    """A new, empty 0600 file: ``path``, else the first free ``path.<epoch>``,
    ``path.<epoch>-<n>``. ``O_EXCL`` (and ``O_NOFOLLOW``): an existing file or link
    is never opened, so a backup is never overwritten."""
    stamp = int(time.time())
    names = [path, path.with_name(f"{path.name}.{stamp}")]
    names += [path.with_name(f"{path.name}.{stamp}-{n}") for n in range(1, 1000)]
    for cand in names:
        try:
            fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        os.chmod(cand, 0o600)  # whatever the umask
        return cand
    raise SchemaError(f"no free name for the migration backup next to {path.name}")


def backup_verified(con: sqlite3.Connection, backup_to: str | os.PathLike) -> tuple[Path, dict[str, int]]:
    """Copy the database with the sqlite3 backup API into a new 0600 file (never
    overwriting one: see ``_create_backup_file``) and check the copy: ``integrity_check``
    and the row count of every version-1 table equal to the source's. The copy is a
    single self-contained file (rollback journal, no ``-wal``). Returns (path, counts)."""
    dest = _create_backup_file(Path(backup_to))
    try:
        src_counts = row_counts(con)
        bcon = sqlite3.connect(str(dest), isolation_level=None)
        try:
            con.backup(bcon)
            bcon.execute("PRAGMA journal_mode=DELETE")
            bad = integrity_ok(bcon)
            if bad is not None:
                raise SchemaError(f"the migration backup {dest.name} failed its integrity check: {bad}")
            got = row_counts(bcon)
        finally:
            bcon.close()
        if got != src_counts:
            raise SchemaError(f"the migration backup {dest.name} does not match the database"
                              f" (rows {got} != {src_counts})")
    except BaseException:
        # a bad copy is not a backup; the name stays taken only by a good one
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise
    return dest, src_counts


def _migrate_v1_to_v2(con: sqlite3.Connection, backup_to: str | os.PathLike | None) -> Path:
    """Schema 1 -> 2 (DESIGN.md §27.6): a verified backup first, then every statement
    in one ``BEGIN IMMEDIATE``, then ``integrity_check`` and the row counts again.
    Any failure rolls back and leaves the version-1 database as it was."""
    if backup_to is None:
        raise SchemaError("schema version 1 needs a migration to version 2, and a migration needs a backup path")
    dest, counts = backup_verified(con, backup_to)
    log.warning("schema migration 1 -> 2: backup written to %s", dest.name)
    con.execute("BEGIN IMMEDIATE")
    try:
        v = schema_version(con)
        if v != 1:
            raise SchemaError(f"the database changed to schema version {v} during the migration")
        if row_counts(con) != counts:
            raise SchemaError("the database changed while its migration backup was made; start again")
        for stmt in V1_TO_V2:
            con.execute(stmt)
        bad = integrity_ok(con)
        if bad is not None:
            raise SchemaError(f"integrity check failed after the migration: {bad}")
        after = row_counts(con)
        if after != counts or row_counts(con, ("remotes",)) != {"remotes": 0}:
            raise SchemaError(f"row counts changed in the migration ({after} != {counts})")
        if schema_version(con) != 2:
            raise SchemaError("the migration did not set schema version 2")
    except BaseException as e:
        if con.in_transaction:  # SQLite may already have rolled back (a full disk, an I/O error)
            con.execute("ROLLBACK")
        if isinstance(e, SchemaError):
            raise SchemaError(f"{e} (the database is unchanged, still schema version 1;"
                              f" backup: {dest.name})") from None
        if isinstance(e, sqlite3.Error):
            raise SchemaError(f"migration to schema version 2 failed: {e} (the database is unchanged,"
                              f" still schema version 1; backup: {dest.name})") from e
        raise
    con.execute("COMMIT")
    log.warning("schema migration 1 -> 2 done")
    return dest


def migrate(con: sqlite3.Connection, backup_to: str | os.PathLike | None = None) -> int:
    """Create the full schema on an empty database, migrate a version-1 one (after a
    verified backup to ``backup_to``, never overwritten), refuse unknown versions."""
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
    if v == 1:
        _migrate_v1_to_v2(con, backup_to)
        v = schema_version(con)
    if v != SCHEMA_VERSION:
        raise SchemaError(
            f"database schema version {v} is not supported (expected {SCHEMA_VERSION})"
        )
    return v


def backup_path_for(db_path: str | os.PathLike) -> Path:
    """``<db>.v1.bak`` next to the database: where the v1 -> v2 migration backs it up."""
    p = Path(db_path)
    return p.with_name(p.name + ".v1.bak")


def open_db(path: str | os.PathLike) -> sqlite3.Connection:
    """connect() + migrate() (a version-1 file is backed up to ``<db>.v1.bak`` first),
    creating the file 0600."""
    p = Path(path)
    if not p.exists():
        fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
    con = connect(p)
    try:
        migrate(con, backup_to=backup_path_for(p))
    except BaseException:
        con.close()
        raise
    return con


def checkpoint(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA wal_checkpoint(PASSIVE)")

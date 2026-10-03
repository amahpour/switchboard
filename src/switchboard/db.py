"""SQLite connection, transactions, the full schema and its migrations (DESIGN.md §4, §27.6).

Every write path runs inside ``with tx(con):`` which issues ``BEGIN IMMEDIATE``.
Connections use ``isolation_level=None``: Python's default transaction mode
silently loses updates on read-then-write (FINDINGS §10 S5).

Schema versions: 1 (0.1.0 and 0.2.0), 2 (remote members: ``participants.host``,
``messages.sender_host``, the ``remotes`` table), 3 (a hosted broker's owner, §31:
``passkeys``, ``web_sessions.via``, ``link_machines``, and the owner's rows in ``meta``)
4 (people, §32: ``people``, a person on passkeys, web sessions, machines and
messages, where NULL is the owner), and 5 (per-person preferences, §29.2).
An older database is migrated once, after a verified 0600 backup (``migrate``), through
every step up to the current version in one transaction; a newer one is refused.
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

from switchboard.models import Room, room_slug

log = logging.getLogger("switchboard.db")

SCHEMA_VERSION = 5

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
  sender_host TEXT,
  sender_person_id INTEGER);
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
  last_seen REAL NOT NULL, expires_at REAL NOT NULL,
  via TEXT,
  person_id INTEGER);

CREATE TABLE remotes(
  name TEXT PRIMARY KEY, config_hash TEXT NOT NULL,
  enabled_at REAL, enabled_via TEXT CHECK(enabled_via IN ('cli','web')),
  blocked_at REAL, blocked_reason TEXT, last_up_at REAL);

CREATE TABLE passkeys(
  credential_id BLOB PRIMARY KEY, public_key BLOB NOT NULL,
  sign_count INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL, aaguid TEXT,
  created_at REAL NOT NULL, last_used_at REAL,
  person_id INTEGER);

CREATE TABLE link_machines(
  name TEXT PRIMARY KEY, key BLOB NOT NULL, key_fp TEXT NOT NULL,
  facts TEXT NOT NULL DEFAULT '{}',
  rooms TEXT NOT NULL DEFAULT '["*"]',
  harnesses TEXT NOT NULL DEFAULT '["claude","codex","cursor","devin"]',
  created_at REAL NOT NULL, approved_at REAL, approved_via TEXT CHECK(approved_via IN ('cli','web')),
  removed_at REAL, last_seen_at REAL,
  person_id INTEGER);

CREATE TABLE people(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE,
  handle BLOB NOT NULL UNIQUE,
  password_hash TEXT, must_reset INTEGER NOT NULL DEFAULT 1, password_expires_at REAL,
  created_at REAL NOT NULL, removed_at REAL);
CREATE UNIQUE INDEX people_active_name ON people(name) WHERE removed_at IS NULL;

CREATE TABLE preferences(
  person_id INTEGER PRIMARY KEY CHECK(person_id >= 0),
  theme TEXT NOT NULL DEFAULT 'system' CHECK(theme IN ('system','light','dark')));
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
    "passkeys",
    "link_machines",
    "people",
    "preferences",
)
# the tables of a version-1 database (0.1.0, 0.2.0) and of a version-2 one (0.3.0 to 0.6.5):
# the rows a migration must keep
V1_TABLES = TABLES[:9]
V2_TABLES = TABLES[:10]
V3_TABLES = TABLES[:12]
V4_TABLES = TABLES[:13]

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
# v2 -> v3 (DESIGN.md §31.2): the same machinery. ``web_sessions.via`` says how a session was
# made (a login link, the claim, a passkey); ``passkeys`` are the owner's; ``link_machines``
# are the machines that dial in (the owner's rows in ``meta`` need no statement).
V2_TO_V3 = (
    "ALTER TABLE web_sessions ADD COLUMN via TEXT",
    "CREATE TABLE passkeys("
    " credential_id BLOB PRIMARY KEY, public_key BLOB NOT NULL,"
    " sign_count INTEGER NOT NULL DEFAULT 0,"
    " name TEXT NOT NULL, aaguid TEXT,"
    " created_at REAL NOT NULL, last_used_at REAL)",
    "CREATE TABLE link_machines("
    " name TEXT PRIMARY KEY, key BLOB NOT NULL, key_fp TEXT NOT NULL,"
    " facts TEXT NOT NULL DEFAULT '{}',"
    " rooms TEXT NOT NULL DEFAULT '[\"*\"]',"
    " harnesses TEXT NOT NULL DEFAULT '[\"claude\",\"codex\",\"cursor\",\"devin\"]',"
    " created_at REAL NOT NULL, approved_at REAL, approved_via TEXT CHECK(approved_via IN ('cli','web')),"
    " removed_at REAL, last_seen_at REAL)",
    "UPDATE meta SET value='3' WHERE key='schema_version'",
)
# v3 -> v4 (DESIGN.md §32.2): the same machinery. A person on every passkey, web session,
# machine and human message, NULL for the owner (so every existing row stays the owner's
# and none is rewritten); ``people`` are the others, each with a password that starts as a
# one-time password (``must_reset``) and passkeys of their own.
V3_TO_V4 = (
    "ALTER TABLE messages ADD COLUMN sender_person_id INTEGER",
    "ALTER TABLE web_sessions ADD COLUMN person_id INTEGER",
    "ALTER TABLE passkeys ADD COLUMN person_id INTEGER",
    "ALTER TABLE link_machines ADD COLUMN person_id INTEGER",
    "CREATE TABLE people("
    " id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE,"
    " handle BLOB NOT NULL UNIQUE,"
    " password_hash TEXT, must_reset INTEGER NOT NULL DEFAULT 1, password_expires_at REAL,"
    " created_at REAL NOT NULL, removed_at REAL)",
    "CREATE UNIQUE INDEX people_active_name ON people(name) WHERE removed_at IS NULL",
    "UPDATE meta SET value='4' WHERE key='schema_version'",
)

# v4 -> v5 (DESIGN.md §29.2): key 0 is the owner, positive ids are people. Defaults
# are virtual until a person chooses a setting, so old databases need no rows rewritten.
V4_TO_V5 = (
    "CREATE TABLE preferences("
    " person_id INTEGER PRIMARY KEY CHECK(person_id >= 0),"
    " theme TEXT NOT NULL DEFAULT 'system' CHECK(theme IN ('system','light','dark')))",
    "UPDATE meta SET value='5' WHERE key='schema_version'",
)


def _steps(frm: int) -> list[tuple[int, tuple[str, ...], tuple[str, ...]]]:
    """The migration steps from schema ``frm`` up to the current one: (to, statements,
    the tables the step adds). Read from the module at run time, never cached."""
    all_steps = {1: (2, V1_TO_V2, ("remotes",)), 2: (3, V2_TO_V3, ("passkeys", "link_machines")),
                 3: (4, V3_TO_V4, ("people",)), 4: (5, V4_TO_V5, ("preferences",))}
    out = []
    v = frm
    while v < SCHEMA_VERSION:
        to, stmts, new = all_steps[v]
        out.append((to, stmts, new))
        v = to
    return out


def tables_of(version: int) -> tuple[str, ...]:
    """The tables a database of schema ``version`` has (the rows a migration keeps)."""
    if version <= 1:
        return V1_TABLES
    if version == 2:
        return V2_TABLES
    if version == 3:
        return V3_TABLES
    if version == 4:
        return V4_TABLES
    return TABLES


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


def _create_backup_file(path: Path, what: str = "migration backup") -> Path:
    """A new, empty 0600 file: ``path``, else the first free ``path.<epoch>``,
    ``path.<epoch>-<n>``. ``O_EXCL`` (and ``O_NOFOLLOW``): an existing file or link
    is never opened, so a backup is never overwritten. ``what`` names it in the error."""
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
    raise SchemaError(f"no free name for the {what} next to {path.name}")


def backup_verified(con: sqlite3.Connection, backup_to: str | os.PathLike, *,
                    tables: tuple[str, ...] = V1_TABLES,
                    what: str = "migration backup") -> tuple[Path, dict[str, int]]:
    """Copy the database with the sqlite3 backup API into a new 0600 file (never
    overwriting one: see ``_create_backup_file``) and check the copy: ``integrity_check``
    and the row count of every table in ``tables`` (the version-1 ones for a migration,
    all of them before a room delete, §28.6) equal to the source's. The copy is a
    single self-contained file (rollback journal, no ``-wal``). ``what`` names it in
    the errors. Returns (path, counts)."""
    dest = _create_backup_file(Path(backup_to), what)
    try:
        src_counts = row_counts(con, tables)
        bcon = sqlite3.connect(str(dest), isolation_level=None)
        try:
            con.backup(bcon)
            bcon.execute("PRAGMA journal_mode=DELETE")
            bad = integrity_ok(bcon)
            if bad is not None:
                raise SchemaError(f"the {what} {dest.name} failed its integrity check: {bad}")
            got = row_counts(bcon, tables)
        finally:
            bcon.close()
        if got != src_counts:
            raise SchemaError(f"the {what} {dest.name} does not match the database"
                              f" (rows {got} != {src_counts})")
    except BaseException:
        # a bad copy is not a backup; the name stays taken only by a good one
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise
    return dest, src_counts


def _migrate(con: sqlite3.Connection, backup_to: str | os.PathLike | None, frm: int) -> Path:
    """Schema ``frm`` -> the current one (DESIGN.md §27.6, §31.2): a verified backup first,
    then every step's statements in one ``BEGIN IMMEDIATE``, then ``integrity_check`` and
    the row counts again: the old tables' rows are all still there and the new tables are
    empty. Any failure rolls back and leaves the database as it was."""
    if backup_to is None:
        raise SchemaError(f"schema version {frm} needs a migration to version {SCHEMA_VERSION}, and a migration"
                          " needs a backup path")
    kept = tables_of(frm)
    dest, counts = backup_verified(con, backup_to, tables=kept)
    log.warning("schema migration %d -> %d: backup written to %s", frm, SCHEMA_VERSION, dest.name)
    con.execute("BEGIN IMMEDIATE")
    try:
        v = schema_version(con)
        if v != frm:
            raise SchemaError(f"the database changed to schema version {v} during the migration")
        if row_counts(con, kept) != counts:
            raise SchemaError("the database changed while its migration backup was made; start again")
        added: list[str] = []
        for to, stmts, new in _steps(frm):
            for stmt in stmts:
                con.execute(stmt)
            if schema_version(con) != to:
                raise SchemaError(f"the migration did not set schema version {to}")
            added += new
        bad = integrity_ok(con)
        if bad is not None:
            raise SchemaError(f"integrity check failed after the migration: {bad}")
        after = row_counts(con, kept)
        empty = {t: 0 for t in added}
        if after != counts or row_counts(con, tuple(added)) != empty:
            raise SchemaError(f"row counts changed in the migration ({after} != {counts})")
        if schema_version(con) != SCHEMA_VERSION:
            raise SchemaError(f"the migration did not set schema version {SCHEMA_VERSION}")
    except BaseException as e:
        if con.in_transaction:  # SQLite may already have rolled back (a full disk, an I/O error)
            con.execute("ROLLBACK")
        if isinstance(e, SchemaError):
            raise SchemaError(f"{e} (the database is unchanged, still schema version {frm};"
                              f" backup: {dest.name})") from None
        if isinstance(e, sqlite3.Error):
            raise SchemaError(f"migration to schema version {SCHEMA_VERSION} failed: {e} (the database is"
                              f" unchanged, still schema version {frm}; backup: {dest.name})") from e
        raise
    con.execute("COMMIT")
    log.warning("schema migration %d -> %d done", frm, SCHEMA_VERSION)
    return dest


def migrate(con: sqlite3.Connection, backup_to: str | os.PathLike | None = None) -> int:
    """Create the full schema on an empty database, migrate an older one (after a
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
    if 1 <= v < SCHEMA_VERSION:
        _migrate(con, backup_to, v)
        v = schema_version(con)
    if v != SCHEMA_VERSION:
        raise SchemaError(
            f"database schema version {v} is not supported (expected {SCHEMA_VERSION})"
        )
    return v


def backup_path_for(db_path: str | os.PathLike, version: int) -> Path:
    """``<db>.v<version>.bak`` next to the database: where the migration of a schema-
    ``version`` database backs it up first (``switchboard.db.v1.bak``, ``.v2.bak``)."""
    p = Path(db_path)
    return p.with_name(f"{p.name}.v{version}.bak")


def delete_backup_path(db_path: str | os.PathLike, room: Room) -> Path:
    """``<db>.delete-<name>-<id>.bak`` next to the database: where ``switchboard rooms
    delete`` backs it up before deleting ``room`` (DESIGN.md §28.6), e.g.
    ``switchboard.db.delete-build-7.bak``. Taken already: ``.<epoch>``, then
    ``.<epoch>-<n>`` (``_create_backup_file``)."""
    p = Path(db_path)
    return p.with_name(f"{p.name}.delete-{room_slug(room.display_name)}-{room.id}.bak")


def open_db(path: str | os.PathLike) -> sqlite3.Connection:
    """connect() + migrate() (an older file is backed up to ``<db>.v<its version>.bak``
    first), creating the file 0600."""
    p = Path(path)
    if not p.exists():
        fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
    con = connect(p)
    try:
        v = schema_version(con)
        migrate(con, backup_to=backup_path_for(p, v) if v else None)
    except BaseException:
        con.close()
        raise
    return con


def checkpoint(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA wal_checkpoint(PASSIVE)")

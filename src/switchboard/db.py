"""SQLite connection, transactions, the full schema and its migrations (DESIGN.md §4, §27.6).

Every write path runs inside ``with tx(con):`` which issues ``BEGIN IMMEDIATE``.
Connections use ``isolation_level=None``: Python's default transaction mode
silently loses updates on read-then-write (FINDINGS §10 S5).

Schema versions: 1 (0.1.0 and 0.2.0), 2 (remote members: ``participants.host``,
``messages.sender_host``, the ``remotes`` table), 3 (a hosted broker's owner, §31:
``passkeys``, ``web_sessions.via``, ``link_machines``, and the owner's rows in ``meta``)
4 (people, §32: ``people``, a person on passkeys, web sessions, machines and
messages, where NULL is the owner), 5 (per-person preferences, §34), 6 (text size, §34),
7 (custom room rules, §35), and so on up to 28 (each step is below); 29 (#385) drops the
``remotes`` and ``link_machines`` tables; 30 (#385) ends the sessions of the local agent path and
drops the process ids and the stdio credential that only it used; 31 (#386) adds the clients that
register to sign an agent in through the browser, and says how each agent token was made.
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

SCHEMA_VERSION = 31

SCHEMA = r"""
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE rooms(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL, created_by TEXT NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0, paused_reason TEXT,
  budget_per_hour INTEGER NOT NULL, budget_remaining INTEGER NOT NULL, budget_window_start REAL NOT NULL,
  budget_notice_window REAL,
  hop_count INTEGER NOT NULL DEFAULT 0, hop_limit INTEGER NOT NULL,
  last_msg_at REAL,
  rules_text TEXT NOT NULL DEFAULT '' CHECK(length(rules_text) <= 2000),
  rules_version INTEGER NOT NULL DEFAULT 1 CHECK(rules_version >= 1),
  private INTEGER NOT NULL DEFAULT 0 CHECK(private IN (0, 1)),
  renamed_from TEXT NOT NULL DEFAULT '[]');

CREATE TABLE participants(
  id INTEGER PRIMARY KEY,
  harness TEXT NOT NULL CHECK(harness IN ('claude','codex','cursor','devin','test','unknown')),
  session_key TEXT NOT NULL,
  session_id TEXT,
  status TEXT NOT NULL DEFAULT 'starting'
     CHECK(status IN ('starting','idle','busy','waiting-approval','offline')),
  status_at REAL, status_src TEXT,
  tier TEXT,
  tier_note TEXT,
  approval_mode TEXT NOT NULL DEFAULT 'unknown' CHECK(approval_mode IN ('bypass','prompting','unknown')),
  away TEXT,
  boundary_seq INTEGER NOT NULL DEFAULT 0,
  gen TEXT, gen_tainted INTEGER NOT NULL DEFAULT 0, rearms_in_gen INTEGER NOT NULL DEFAULT 0,
  hooks_seen_at REAL, last_say_at REAL, created_at REAL NOT NULL, last_seen REAL, ended_at REAL,
  host TEXT NOT NULL DEFAULT '',
  status_line TEXT,
  status_line_at REAL,
  UNIQUE(harness, session_key));

CREATE TABLE memberships(
  id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
  participant_id INTEGER NOT NULL REFERENCES participants(id),
  screen_name TEXT NOT NULL COLLATE NOCASE,
  joined_at REAL NOT NULL, join_msg_id INTEGER NOT NULL,
  left_at REAL, left_reason TEXT,
  kicked INTEGER NOT NULL DEFAULT 0,
  held INTEGER NOT NULL DEFAULT 0, held_at REAL,
  cursor_id INTEGER NOT NULL DEFAULT 0,
  peer_batch_boundary INTEGER NOT NULL DEFAULT -1,
  rules_seen INTEGER NOT NULL DEFAULT 0 CHECK(rules_seen >= 0),
  join_code_hash TEXT);
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

CREATE TABLE passkeys(
  credential_id BLOB PRIMARY KEY, public_key BLOB NOT NULL,
  sign_count INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL, aaguid TEXT,
  created_at REAL NOT NULL, last_used_at REAL,
  person_id INTEGER);

CREATE TABLE people(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE,
  handle BLOB NOT NULL UNIQUE,
  password_hash TEXT, must_reset INTEGER NOT NULL DEFAULT 1, password_expires_at REAL,
  created_at REAL NOT NULL, removed_at REAL,
  email TEXT CHECK(email IS NULL OR length(email) <= 254),
  first_name TEXT CHECK(first_name IS NULL OR length(first_name) <= 64),
  last_name TEXT CHECK(last_name IS NULL OR length(last_name) <= 64));
CREATE UNIQUE INDEX people_active_name ON people(name) WHERE removed_at IS NULL;
CREATE UNIQUE INDEX people_email ON people(email) WHERE removed_at IS NULL AND email IS NOT NULL;

CREATE TABLE preferences(
  person_id INTEGER PRIMARY KEY CHECK(person_id >= 0),
  theme TEXT NOT NULL DEFAULT 'system' CHECK(theme IN ('system','light','dark')),
  text_size TEXT NOT NULL DEFAULT 'default' CHECK(text_size IN ('small','default','large','larger')),
  room_rules TEXT NOT NULL DEFAULT '' CHECK(length(room_rules) <= 2000),
  budget_per_hour INTEGER CHECK(budget_per_hour IS NULL OR (budget_per_hour BETWEEN 0 AND 1000000)),
  hop_limit INTEGER CHECK(hop_limit IS NULL OR (hop_limit BETWEEN 0 AND 1000)));

CREATE TABLE room_people(
  room_id INTEGER NOT NULL REFERENCES rooms(id), person_id INTEGER NOT NULL CHECK(person_id >= 0),
  added_by INTEGER NOT NULL CHECK(added_by >= 0), added_at REAL NOT NULL,
  PRIMARY KEY(room_id, person_id));

CREATE TABLE reviews(
  id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
  url TEXT NOT NULL CHECK(length(url) <= 500), head TEXT NOT NULL DEFAULT '' CHECK(length(head) <= 64),
  opened_by TEXT NOT NULL, opened_at REAL NOT NULL, closed_at REAL, posted_at REAL, posted_by TEXT,
  pr_state TEXT NOT NULL DEFAULT '' CHECK(pr_state IN ('', 'open', 'merged', 'closed')),
  pr_threads INTEGER CHECK(pr_threads IS NULL OR pr_threads BETWEEN 0 AND 999),
  pr_ci TEXT NOT NULL DEFAULT '' CHECK(pr_ci IN ('', 'passing', 'failing', 'running', 'none')),
  reported_by TEXT NOT NULL DEFAULT '' CHECK(length(reported_by) <= 100), reported_at REAL,
  closed_by TEXT NOT NULL DEFAULT '' CHECK(length(closed_by) <= 100),
  reopened_by TEXT NOT NULL DEFAULT '' CHECK(length(reopened_by) <= 100), reopened_at REAL);
CREATE INDEX reviews_room ON reviews(room_id, id);
CREATE TABLE review_items(
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL REFERENCES reviews(id),
  kind TEXT NOT NULL CHECK(kind IN ('finding','question')), n INTEGER NOT NULL CHECK(n > 0),
  state TEXT NOT NULL
    CHECK(state IN ('raised','contested','conceded','fixed','dropped','open','answered')),
  title TEXT NOT NULL CHECK(length(title) <= 200),
  detail TEXT NOT NULL DEFAULT '' CHECK(length(detail) <= 4000),
  file TEXT NOT NULL DEFAULT '' CHECK(length(file) <= 300),
  lines TEXT NOT NULL DEFAULT '' CHECK(length(lines) <= 40),
  raised_by TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', commit_sha TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '' CHECK(length(reason) <= 500), options TEXT NOT NULL DEFAULT '[]',
  recommend INTEGER, answer TEXT NOT NULL DEFAULT '' CHECK(length(answer) <= 500),
  answered_by TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
  proof TEXT NOT NULL DEFAULT '' CHECK(length(proof) <= 500),
  confirmed_by TEXT NOT NULL DEFAULT '' CHECK(length(confirmed_by) <= 100),
  outdated_head TEXT NOT NULL DEFAULT '' CHECK(length(outdated_head) <= 64),
  deferred_to TEXT NOT NULL DEFAULT '' CHECK(length(deferred_to) <= 200),
  insisted TEXT NOT NULL DEFAULT '' CHECK(length(insisted) <= 500),
  UNIQUE(review_id, kind, n));
CREATE TABLE review_picks(
  item_id INTEGER NOT NULL REFERENCES review_items(id),
  agent TEXT NOT NULL CHECK(length(agent) BETWEEN 1 AND 100),
  choice INTEGER NOT NULL CHECK(choice BETWEEN 0 AND 3), picked_at REAL NOT NULL,
  PRIMARY KEY(item_id, agent));

CREATE TABLE claims(
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  key_norm TEXT NOT NULL CHECK(length(key_norm) BETWEEN 1 AND 512),
  key TEXT NOT NULL CHECK(length(key) BETWEEN 1 AND 120),
  membership_id INTEGER NOT NULL REFERENCES memberships(id),
  expires_at REAL NOT NULL,
  PRIMARY KEY(room_id, key_norm)) WITHOUT ROWID;
CREATE INDEX claims_member ON claims(membership_id);
CREATE TABLE pins(
  room_id INTEGER NOT NULL REFERENCES rooms(id), message_id INTEGER NOT NULL REFERENCES messages(id),
  pinned_by TEXT NOT NULL CHECK(length(pinned_by) <= 100), pinned_at REAL NOT NULL,
  PRIMARY KEY(room_id, message_id));
CREATE TABLE agent_tokens(
  id INTEGER PRIMARY KEY, person_id INTEGER,
  device TEXT NOT NULL CHECK(length(device) BETWEEN 1 AND 24),
  token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
  created_at REAL NOT NULL, last_used_at REAL, revoked_at REAL,
  via TEXT NOT NULL DEFAULT 'token' CHECK(via IN ('token', 'oauth')),
  client_name TEXT CHECK(client_name IS NULL OR length(client_name) <= 80));
CREATE UNIQUE INDEX agent_tokens_device ON agent_tokens(device) WHERE revoked_at IS NULL;
CREATE TABLE oauth_clients(
  client_id TEXT PRIMARY KEY CHECK(length(client_id) BETWEEN 8 AND 64),
  client_name TEXT NOT NULL CHECK(length(client_name) BETWEEN 1 AND 80),
  redirect_uris TEXT NOT NULL CHECK(length(redirect_uris) <= 2000),
  created_at REAL NOT NULL);
"""

# The tables of each schema version that a migration must keep the rows of. The older versions'
# lists name the two tables 29 dropped (``remotes`` and ``link_machines``: #385), because a backup
# is checked table by table before the steps run; ``TABLES`` is what the current schema has.
V1_TABLES = (
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
V2_TABLES = (*V1_TABLES, "remotes")
V3_TABLES = (*V2_TABLES, "passkeys", "link_machines")
V4_TABLES = (*V3_TABLES, "people")
V8_TABLES = (*V4_TABLES, "preferences")
V14_TABLES = (*V8_TABLES, "reviews", "review_items")  # before room_people (v15)
V20_TABLES = (*V14_TABLES, "room_people")  # before claims (v21)
V21_TABLES = (*V20_TABLES, "claims")  # before pins (v22)
V24_TABLES = (*V21_TABLES, "pins")  # before agent_tokens (v25)
V25_TABLES = (*V24_TABLES, "agent_tokens")  # before review_picks (v26)
V28_TABLES = (*V25_TABLES, "review_picks")  # before 29 took the two retired tables away
# the tables a step drops, by the version it migrates to
DROPPED_AT: dict[int, tuple[str, ...]] = {29: ("remotes", "link_machines")}
V30_TABLES = tuple(t for t in V28_TABLES if t not in DROPPED_AT[29])  # before oauth_clients (v31)
TABLES = (*V30_TABLES, "oauth_clients")

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
    ' harnesses TEXT NOT NULL DEFAULT \'["claude","codex","cursor","devin"]\','
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

# v5 -> v6 (#79): existing choices keep their theme; every person starts at the former size.
V5_TO_V6 = (
    "ALTER TABLE preferences ADD COLUMN text_size TEXT NOT NULL DEFAULT 'default'"
    " CHECK(text_size IN ('small','default','large','larger'))",
    "UPDATE meta SET value='6' WHERE key='schema_version'",
)

# v6 -> v7 (#105): existing rooms and people keep an empty rules text.
V6_TO_V7 = (
    "ALTER TABLE rooms ADD COLUMN rules_text TEXT NOT NULL DEFAULT '' CHECK(length(rules_text) <= 2000)",
    "ALTER TABLE preferences ADD COLUMN room_rules TEXT NOT NULL DEFAULT ''"
    " CHECK(length(room_rules) <= 2000)",
    "UPDATE meta SET value='7' WHERE key='schema_version'",
)

# v7 -> v8 (#140): existing members receive the current room rules once on their next batch.
V7_TO_V8 = (
    "ALTER TABLE rooms ADD COLUMN rules_version INTEGER NOT NULL DEFAULT 1 CHECK(rules_version >= 1)",
    "ALTER TABLE memberships ADD COLUMN rules_seen INTEGER NOT NULL DEFAULT 0 CHECK(rules_seen >= 0)",
    "UPDATE meta SET value='8' WHERE key='schema_version'",
)

# v8 -> v9 (#80, DESIGN.md §37.3): a room's review boards and their items. New tables only.
V8_TO_V9 = (
    "CREATE TABLE reviews("
    " id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),"
    " url TEXT NOT NULL CHECK(length(url) <= 500), head TEXT NOT NULL DEFAULT '' CHECK(length(head) <= 64),"
    " opened_by TEXT NOT NULL, opened_at REAL NOT NULL, closed_at REAL, posted_at REAL, posted_by TEXT)",
    "CREATE INDEX reviews_room ON reviews(room_id, id)",
    "CREATE TABLE review_items("
    " id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL REFERENCES reviews(id),"
    " kind TEXT NOT NULL CHECK(kind IN ('finding','question')), n INTEGER NOT NULL CHECK(n > 0),"
    " state TEXT NOT NULL"
    " CHECK(state IN ('raised','contested','conceded','fixed','dropped','open','answered')),"
    " title TEXT NOT NULL CHECK(length(title) <= 200),"
    " detail TEXT NOT NULL DEFAULT '' CHECK(length(detail) <= 4000),"
    " file TEXT NOT NULL DEFAULT '' CHECK(length(file) <= 300),"
    " lines TEXT NOT NULL DEFAULT '' CHECK(length(lines) <= 40),"
    " raised_by TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', commit_sha TEXT NOT NULL DEFAULT '',"
    " reason TEXT NOT NULL DEFAULT '' CHECK(length(reason) <= 500), options TEXT NOT NULL DEFAULT '[]',"
    " recommend INTEGER, answer TEXT NOT NULL DEFAULT '' CHECK(length(answer) <= 500),"
    " answered_by TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,"
    " UNIQUE(review_id, kind, n))",
    "UPDATE meta SET value='9' WHERE key='schema_version'",
)

# v9 -> v10 (#70, DESIGN.md §38.3): the Google email a person signs in with, set by the admin.
# The owner's is a meta row, needing no statement. Every existing person starts without one.
V9_TO_V10 = (
    "ALTER TABLE people ADD COLUMN google_email TEXT"
    " CHECK(google_email IS NULL OR length(google_email) <= 254)",
    "CREATE UNIQUE INDEX people_google_email ON people(google_email)"
    " WHERE removed_at IS NULL AND google_email IS NOT NULL",
    "UPDATE meta SET value='10' WHERE key='schema_version'",
)

# v10 -> v11 (#178, DESIGN.md §31.8): who approved a machine (a person's id; NULL: the owner, or
# approved before this version), beside ``person_id``, who paired it. Removing a person removes
# the machines they paired or approved.
V10_TO_V11 = (
    "ALTER TABLE link_machines ADD COLUMN approved_by INTEGER",
    "UPDATE meta SET value='11' WHERE key='schema_version'",
)

# v11 -> v12 (#192, DESIGN.md §39): a person's email is who they are. The Google email (#70)
# becomes it, for people and for the owner, so everyone who signs in with Google keeps doing so.
V11_TO_V12 = (
    "ALTER TABLE people RENAME COLUMN google_email TO email",
    "DROP INDEX people_google_email",
    "CREATE UNIQUE INDEX people_email ON people(email) WHERE removed_at IS NULL AND email IS NOT NULL",
    "UPDATE meta SET key='owner_email' WHERE key='owner_google_email'",
    "UPDATE meta SET value='12' WHERE key='schema_version'",
)

# v12 -> v13 (#192, DESIGN.md §39.6): a person's first and last name, for the People sheet,
# Members and the invite; the name in the rooms stays the short handle @mentions use. The
# owner's are meta rows (owner_first_name, owner_last_name), as their email is.
V12_TO_V13 = (
    "ALTER TABLE people ADD COLUMN first_name TEXT CHECK(first_name IS NULL OR length(first_name) <= 64)",
    "ALTER TABLE people ADD COLUMN last_name TEXT CHECK(last_name IS NULL OR length(last_name) <= 64)",
    "UPDATE meta SET value='13' WHERE key='schema_version'",
)

# v13 -> v14 (#131, DESIGN.md §42): a person's own default wake budget per hour and hop limit
# for a new room they create, NULL (not set) until they choose one; a new room then falls back
# to `[delivery]` in config.toml, then the built-in default. Existing people start unset, which
# keeps today's room-creation behaviour unchanged for everyone who hasn't visited Settings.
V13_TO_V14 = (
    "ALTER TABLE preferences ADD COLUMN budget_per_hour INTEGER"
    " CHECK(budget_per_hour IS NULL OR (budget_per_hour >= 0 AND budget_per_hour <= 1000000))",
    "ALTER TABLE preferences ADD COLUMN hop_limit INTEGER"
    " CHECK(hop_limit IS NULL OR (hop_limit >= 0 AND hop_limit <= 1000))",
    "UPDATE meta SET value='14' WHERE key='schema_version'",
)

# v14 -> v15 (#74, DESIGN.md §44): a room can be private, seen only by its people. Every
# existing room stays public (the default), so nothing anyone sees changes; ``room_people``
# lists a private room's people by id, 0 being the owner, as ``preferences`` keys them.
V14_TO_V15 = (
    "ALTER TABLE rooms ADD COLUMN private INTEGER NOT NULL DEFAULT 0 CHECK(private IN (0, 1))",
    "CREATE TABLE room_people("
    " room_id INTEGER NOT NULL REFERENCES rooms(id), person_id INTEGER NOT NULL CHECK(person_id >= 0),"
    " added_by INTEGER NOT NULL CHECK(added_by >= 0), added_at REAL NOT NULL,"
    " PRIMARY KEY(room_id, person_id))",
    "UPDATE meta SET value='15' WHERE key='schema_version'",
)
# the join code each membership was given, hashed, so the same agent can take its name back
# after a restart (#304, DESIGN.md §6.1); NULL for one made before
V15_TO_V16 = (
    "ALTER TABLE memberships ADD COLUMN join_code_hash TEXT",
    "UPDATE meta SET value='16' WHERE key='schema_version'",
)
V16_TO_V17 = (
    "ALTER TABLE participants ADD COLUMN status_line TEXT",
    "ALTER TABLE participants ADD COLUMN status_line_at REAL",
    "UPDATE meta SET value='17' WHERE key='schema_version'",
)
# v17 -> v18 (#316): keep former names on the room so existing MCP sessions can continue
# using their old room key and a new join can be directed to the current name.
V17_TO_V18 = (
    "ALTER TABLE rooms ADD COLUMN renamed_from TEXT NOT NULL DEFAULT '[]'",
    "UPDATE meta SET value='18' WHERE key='schema_version'",
)

# v18 -> v19 (#251): fixed findings carry their proof and wait for confirmation.
V18_TO_V19 = (
    "ALTER TABLE review_items ADD COLUMN proof TEXT NOT NULL DEFAULT '' CHECK(length(proof) <= 500)",
    "ALTER TABLE review_items ADD COLUMN confirmed_by TEXT NOT NULL DEFAULT ''"
    " CHECK(length(confirmed_by) <= 100)",
    "UPDATE review_items SET confirmed_by='legacy' WHERE state='fixed'",
    "UPDATE meta SET value='19' WHERE key='schema_version'",
)

# v19 -> v20 (#291): an unsettled finding can be flagged when the board's head changes.
V19_TO_V20 = (
    "ALTER TABLE review_items ADD COLUMN outdated_head TEXT NOT NULL DEFAULT ''"
    " CHECK(length(outdated_head) <= 64)",
    "UPDATE meta SET value='20' WHERE key='schema_version'",
)

# v20 -> v21 (#293): claims are advisory, per room, and tied to one membership.
V20_TO_V21 = (
    "CREATE TABLE claims("
    " room_id INTEGER NOT NULL REFERENCES rooms(id),"
    " key_norm TEXT NOT NULL CHECK(length(key_norm) BETWEEN 1 AND 512),"
    " key TEXT NOT NULL CHECK(length(key) BETWEEN 1 AND 120),"
    " membership_id INTEGER NOT NULL REFERENCES memberships(id),"
    " expires_at REAL NOT NULL,"
    " PRIMARY KEY(room_id, key_norm)) WITHOUT ROWID",
    "CREATE INDEX claims_member ON claims(membership_id)",
    "UPDATE meta SET value='21' WHERE key='schema_version'",
)

# v21 -> v22 (#292, DESIGN.md §48): a room's pinned decisions, at most five, by message. The
# table is new, so every existing room starts with none.
V21_TO_V22 = (
    "CREATE TABLE pins("
    " room_id INTEGER NOT NULL REFERENCES rooms(id), message_id INTEGER NOT NULL REFERENCES messages(id),"
    " pinned_by TEXT NOT NULL CHECK(length(pinned_by) <= 100), pinned_at REAL NOT NULL,"
    " PRIMARY KEY(room_id, message_id))",
    "UPDATE meta SET value='22' WHERE key='schema_version'",
)

# v22 -> v23 (#371, DESIGN.md §37.2): a finding deferred to a follow-up ticket. It is a dropped
# finding with the ticket it went to, so everything that settles a dropped one settles it too.
V22_TO_V23 = (
    "ALTER TABLE review_items ADD COLUMN deferred_to TEXT NOT NULL DEFAULT ''"
    " CHECK(length(deferred_to) <= 200)",
    "UPDATE meta SET value='23' WHERE key='schema_version'",
)

# v23 -> v24 (#291, DESIGN.md §37.11): what an agent read off the pull request with its own gh or
# glab and reported to the board: its state, the threads still open on it, its CI, by whom, when.
V23_TO_V24 = (
    "ALTER TABLE reviews ADD COLUMN pr_state TEXT NOT NULL DEFAULT ''"
    " CHECK(pr_state IN ('', 'open', 'merged', 'closed'))",
    "ALTER TABLE reviews ADD COLUMN pr_threads INTEGER"
    " CHECK(pr_threads IS NULL OR pr_threads BETWEEN 0 AND 999)",
    "ALTER TABLE reviews ADD COLUMN pr_ci TEXT NOT NULL DEFAULT ''"
    " CHECK(pr_ci IN ('', 'passing', 'failing', 'running', 'none'))",
    "ALTER TABLE reviews ADD COLUMN reported_by TEXT NOT NULL DEFAULT '' CHECK(length(reported_by) <= 100)",
    "ALTER TABLE reviews ADD COLUMN reported_at REAL",
    "UPDATE meta SET value='24' WHERE key='schema_version'",
)

# v24 -> v25 (#382, DESIGN.md §49.1): agent tokens, a person's token for the agents on one device.
# The table is new, so nobody has one until they make it; one live token per device name.
V24_TO_V25 = (
    "CREATE TABLE agent_tokens("
    " id INTEGER PRIMARY KEY, person_id INTEGER,"
    " device TEXT NOT NULL CHECK(length(device) BETWEEN 1 AND 24),"
    " token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),"
    " created_at REAL NOT NULL, last_used_at REAL, revoked_at REAL)",
    "CREATE UNIQUE INDEX agent_tokens_device ON agent_tokens(device) WHERE revoked_at IS NULL",
    "UPDATE meta SET value='25' WHERE key='schema_version'",
)

# v25 -> v26 (#423, DESIGN.md §37.12): the pick each agent made on a question's or a finding's
# options, so that "recommended" is computed from every agent's pick instead of claimed by one.
# The table is new, so nothing is rewritten: the asker's own pick is still its ``recommend``.
V25_TO_V26 = (
    "CREATE TABLE review_picks("
    " item_id INTEGER NOT NULL REFERENCES review_items(id),"
    " agent TEXT NOT NULL CHECK(length(agent) BETWEEN 1 AND 100),"
    " choice INTEGER NOT NULL CHECK(choice BETWEEN 0 AND 3), picked_at REAL NOT NULL,"
    " PRIMARY KEY(item_id, agent))",
    "UPDATE meta SET value='26' WHERE key='schema_version'",
)

# v26 -> v27 (#424, DESIGN.md §37.13): the reason a finding's raiser gives for insisting on it after
# it was contested. A contested finding with one has both sides' case made, and a person decides;
# without one it is still its raiser's to answer. A finding contested before this was already waiting
# on a person, and its raiser was never asked to answer, so it keeps waiting there: it is marked
# insisted with a line saying so (``reviews.LEGACY_INSISTED``; a test pins the two equal).
V26_TO_V27 = (
    "ALTER TABLE review_items ADD COLUMN insisted TEXT NOT NULL DEFAULT '' CHECK(length(insisted) <= 500)",
    "UPDATE review_items SET insisted='(contested before agents could insist: a person decides)'"
    " WHERE kind='finding' AND state='contested'",
    "UPDATE meta SET value='27' WHERE key='schema_version'",
)

# v27 -> v28 (#445, DESIGN.md §37.14): a closed board is kept, and a room holds several. A board
# says who closed it (``closed_by``) and who opened it again (``reopened_by``, ``reopened_at``), and
# every review event names its board (``data.board``, the review's id): F1 and Q1 are on every board,
# so a card's history and a handoff's decisions can no longer be read off the room's events by label
# alone. The events already stored are named by the board that was newest in their room when they
# were written, which was the room's only one.
V27_TO_V28 = (
    "ALTER TABLE reviews ADD COLUMN closed_by TEXT NOT NULL DEFAULT '' CHECK(length(closed_by) <= 100)",
    "ALTER TABLE reviews ADD COLUMN reopened_by TEXT NOT NULL DEFAULT '' CHECK(length(reopened_by) <= 100)",
    "ALTER TABLE reviews ADD COLUMN reopened_at REAL",
    "UPDATE events SET data=json_set(data, '$.board',"
    " (SELECT r.id FROM reviews r WHERE r.room_id=events.room_id AND r.opened_at<=events.ts"
    "  ORDER BY r.opened_at DESC, r.id DESC LIMIT 1))"
    " WHERE kind='review' AND room_id IS NOT NULL AND json_valid(data) AND json_type(data)='object'"
    " AND json_extract(data, '$.board') IS NULL AND json_extract(data, '$.reviewer') IS NULL"
    " AND EXISTS (SELECT 1 FROM reviews r WHERE r.room_id=events.room_id AND r.opened_at<=events.ts)",
    "UPDATE meta SET value='28' WHERE key='schema_version'",
)


# v28 -> v29 (#385, DESIGN.md §27, §31.7 retired): SSH remotes and the machines that dial in are gone, so
# their two tables go. A session on one of those hosts (a host name and a real MCP pid: a session over
# HTTPS has pid 0) has nothing left to reach it, so it ends as a session end does (``Store.end_membership``,
# ``end_participant``): its membership leaves with reason ``retired``, its open deliveries are revoked,
# its offered batches cancelled, and it goes offline. Its claims stay, as a claim of an ended membership
# does until the next ``claim`` call deletes it. Nothing else is touched, and no row is deleted.
_RETIRED_HOST_SESSION = "p.ended_at IS NULL AND p.host != '' AND p.mcp_pid IS NOT 0"
_NOW = "CAST(strftime('%s','now') AS REAL)"
V28_TO_V29 = (
    "UPDATE deliveries SET state='revoked' WHERE state IN ('pending','offered','in_context')"
    " AND membership_id IN (SELECT m.id FROM memberships m JOIN participants p ON p.id=m.participant_id"
    f" WHERE m.left_at IS NULL AND {_RETIRED_HOST_SESSION})",
    f"UPDATE batches SET state='cancelled', expired_at={_NOW}, expire_reason='retired' WHERE state='offered'"
    " AND membership_id IN (SELECT m.id FROM memberships m JOIN participants p ON p.id=m.participant_id"
    f" WHERE m.left_at IS NULL AND {_RETIRED_HOST_SESSION})",
    f"UPDATE memberships SET left_at={_NOW}, left_reason='retired', cred_hash=NULL WHERE left_at IS NULL"
    f" AND participant_id IN (SELECT p.id FROM participants p WHERE {_RETIRED_HOST_SESSION})",
    f"UPDATE participants SET ended_at={_NOW}, status='offline', status_at={_NOW}, status_src='retired'"
    " WHERE id IN (SELECT p.id FROM participants p"
    f" WHERE {_RETIRED_HOST_SESSION})",
    "DROP TABLE remotes",
    "DROP TABLE link_machines",
    "UPDATE meta SET value='29' WHERE key='schema_version'",
)


# v29 -> v30 (#385, DESIGN.md §49.8): the local agent path is gone, the per-session MCP server and the
# hook script, so a session that reached the broker through it (a real MCP pid, or no device host) has
# no way to reconnect. It ends as a session end does, as in v29: its membership leaves with reason
# ``retired``, its open deliveries are revoked, its offered batches cancelled, and it goes offline. A
# session over HTTPS (a device host and MCP pid 0) is untouched. Then the columns only that path used
# go: the process ids and the MCP server's claims (``agent_pid`` ... ``mcp_start``, ``claude_socket``,
# ``env_leak``), Cursor's bind state and join nonce, Codex's thread proof, the push and follow-up
# counters, and the stdio credential (``memberships.cred_hash``), and the index on the MCP pid. No row
# is deleted. SQLite's ``DROP COLUMN`` (3.35) rewrites the table in place, inside the migration's one
# transaction.
_LOCAL_SESSION = "p.ended_at IS NULL AND NOT (p.host != '' AND p.mcp_pid IS 0)"
V29_TO_V30 = (
    "UPDATE deliveries SET state='revoked' WHERE state IN ('pending','offered','in_context')"
    " AND membership_id IN (SELECT m.id FROM memberships m JOIN participants p ON p.id=m.participant_id"
    f" WHERE m.left_at IS NULL AND {_LOCAL_SESSION})",
    f"UPDATE batches SET state='cancelled', expired_at={_NOW}, expire_reason='retired' WHERE state='offered'"
    " AND membership_id IN (SELECT m.id FROM memberships m JOIN participants p ON p.id=m.participant_id"
    f" WHERE m.left_at IS NULL AND {_LOCAL_SESSION})",
    f"UPDATE memberships SET left_at={_NOW}, left_reason='retired' WHERE left_at IS NULL"
    f" AND participant_id IN (SELECT p.id FROM participants p WHERE {_LOCAL_SESSION})",
    f"UPDATE participants SET ended_at={_NOW}, status='offline', status_at={_NOW}, status_src='retired'"
    f" WHERE id IN (SELECT p.id FROM participants p WHERE {_LOCAL_SESSION})",
    "DROP INDEX participants_host_mcp",
    "ALTER TABLE participants DROP COLUMN agent_pid",
    "ALTER TABLE participants DROP COLUMN agent_start",
    "ALTER TABLE participants DROP COLUMN mcp_pid",
    "ALTER TABLE participants DROP COLUMN mcp_start",
    "ALTER TABLE participants DROP COLUMN claude_socket",
    "ALTER TABLE participants DROP COLUMN bind_state",
    "ALTER TABLE participants DROP COLUMN bind_nonce",
    "ALTER TABLE participants DROP COLUMN thread_proof",
    "ALTER TABLE participants DROP COLUMN env_leak",
    "ALTER TABLE participants DROP COLUMN last_loop_count",
    "ALTER TABLE participants DROP COLUMN unconfirmed_followups",
    "ALTER TABLE participants DROP COLUMN push_expiries",
    "ALTER TABLE memberships DROP COLUMN cred_hash",
    "UPDATE meta SET value='30' WHERE key='schema_version'",
)


# v30 -> v31 (#386, DESIGN.md §49.9): browser sign-in for agents. An agent token now says how it was
# made (typed in Settings, ``token``, or a browser sign-in, ``oauth``) and, for a sign-in, the name the
# agent called itself; the old ones are all ``token``. ``oauth_clients`` holds the clients that
# registered (RFC 7591) and have not been pruned: new, so empty.
V30_TO_V31 = (
    "ALTER TABLE agent_tokens ADD COLUMN via TEXT NOT NULL DEFAULT 'token' CHECK(via IN ('token', 'oauth'))",
    "ALTER TABLE agent_tokens ADD COLUMN client_name TEXT"
    " CHECK(client_name IS NULL OR length(client_name) <= 80)",
    "CREATE TABLE oauth_clients("
    " client_id TEXT PRIMARY KEY CHECK(length(client_id) BETWEEN 8 AND 64),"
    " client_name TEXT NOT NULL CHECK(length(client_name) BETWEEN 1 AND 80),"
    " redirect_uris TEXT NOT NULL CHECK(length(redirect_uris) <= 2000),"
    " created_at REAL NOT NULL)",
    "UPDATE meta SET value='31' WHERE key='schema_version'",
)


def _steps(frm: int) -> list[tuple[int, tuple[str, ...], tuple[str, ...]]]:
    """The migration steps from schema ``frm`` up to the current one: (to, statements,
    the tables the step adds). Read from the module at run time, never cached."""
    all_steps = {
        1: (2, V1_TO_V2, ("remotes",)),
        2: (3, V2_TO_V3, ("passkeys", "link_machines")),
        3: (4, V3_TO_V4, ("people",)),
        4: (5, V4_TO_V5, ("preferences",)),
        5: (6, V5_TO_V6, ()),
        6: (7, V6_TO_V7, ()),
        7: (8, V7_TO_V8, ()),
        8: (9, V8_TO_V9, ("reviews", "review_items")),
        9: (10, V9_TO_V10, ()),
        10: (11, V10_TO_V11, ()),
        11: (12, V11_TO_V12, ()),
        12: (13, V12_TO_V13, ()),
        13: (14, V13_TO_V14, ()),
        14: (15, V14_TO_V15, ("room_people",)),
        15: (16, V15_TO_V16, ()),
        16: (17, V16_TO_V17, ()),
        17: (18, V17_TO_V18, ()),
        18: (19, V18_TO_V19, ()),
        19: (20, V19_TO_V20, ()),
        20: (21, V20_TO_V21, ("claims",)),
        21: (22, V21_TO_V22, ("pins",)),
        22: (23, V22_TO_V23, ()),
        23: (24, V23_TO_V24, ()),
        24: (25, V24_TO_V25, ("agent_tokens",)),
        25: (26, V25_TO_V26, ("review_picks",)),
        26: (27, V26_TO_V27, ()),
        27: (28, V27_TO_V28, ()),
        28: (29, V28_TO_V29, ()),
        29: (30, V29_TO_V30, ()),
        30: (31, V30_TO_V31, ("oauth_clients",)),
    }
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
    if version <= 8:
        return V8_TABLES
    if version <= 14:
        return V14_TABLES
    if version <= 20:
        return V20_TABLES
    if version <= 21:
        return V21_TABLES
    if version <= 24:
        return V24_TABLES
    if version <= 25:
        return V25_TABLES
    if version <= 28:
        return V28_TABLES
    if version <= 30:
        return V30_TABLES
    return TABLES


def kept(tables: tuple[str, ...]) -> tuple[str, ...]:
    """``tables`` without the ones a later migration drops (``DROPPED_AT``)."""
    gone = {t for dropped in DROPPED_AT.values() for t in dropped}
    return tuple(t for t in tables if t not in gone)


def kept_tables(version: int) -> tuple[str, ...]:
    """The tables of a ``version`` database that are still there after a migration to the current
    one: the rows of these are what a migration must keep."""
    return kept(tables_of(version))


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
        con = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None, check_same_thread=False)
    else:
        con = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    if not readonly:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def connect_query_only(path: str | os.PathLike) -> sqlite3.Connection:
    """A connection that only reads and never creates the database (``switchboard report``,
    #167): the file must already exist (the caller checks) and every write is refused.

    Not ``mode=ro``: after a clean broker stop SQLite has removed the WAL's ``-shm`` file, and
    a true read-only connection can't recreate it (the open fails). ``mode=rw`` with
    ``query_only`` can, and still refuses every write. The ``rooms`` table is in every schema
    version, so a count from it also checks the connection can actually read."""
    con = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=rw", uri=True, timeout=5.0)
    con.execute("PRAGMA query_only = ON")
    con.execute("SELECT COUNT(*) FROM rooms").fetchone()
    con.row_factory = sqlite3.Row
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
    row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
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


def backup_verified(
    con: sqlite3.Connection,
    backup_to: str | os.PathLike,
    *,
    tables: tuple[str, ...] = V1_TABLES,
    what: str = "migration backup",
) -> tuple[Path, dict[str, int]]:
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
            raise SchemaError(
                f"the {what} {dest.name} does not match the database (rows {got} != {src_counts})"
            )
    except BaseException:
        # a bad copy is not a backup; the name stays taken only by a good one
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise
    return dest, src_counts


def _migrate(con: sqlite3.Connection, backup_to: str | os.PathLike | None, frm: int) -> Path:
    """Schema ``frm`` -> the current one (DESIGN.md §27.6, §31.2): a verified backup first,
    then every step's statements in one ``BEGIN IMMEDIATE``, then ``integrity_check`` and
    the row counts again: the rows of every old table that is still there are all still there
    (``kept_tables``: a step may drop a table, which the backup still counted) and the new tables
    are empty. Any failure rolls back and leaves the database as it was."""
    if backup_to is None:
        raise SchemaError(
            f"schema version {frm} needs a migration to version {SCHEMA_VERSION}, and a migration"
            " needs a backup path"
        )
    old_tables = tables_of(frm)
    dest, counts = backup_verified(con, backup_to, tables=old_tables)
    log.warning("schema migration %d -> %d: backup written to %s", frm, SCHEMA_VERSION, dest.name)
    con.execute("BEGIN IMMEDIATE")
    try:
        v = schema_version(con)
        if v != frm:
            raise SchemaError(f"the database changed to schema version {v} during the migration")
        if row_counts(con, old_tables) != counts:
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
        survivors = kept_tables(frm)
        after = row_counts(con, survivors)
        made = kept(tuple(added))  # a table made by one step and dropped by a later one is gone
        empty = {t: 0 for t in made}
        if after != {t: counts[t] for t in survivors} or row_counts(con, made) != empty:
            raise SchemaError(f"row counts changed in the migration ({after} != {counts})")
        if schema_version(con) != SCHEMA_VERSION:
            raise SchemaError(f"the migration did not set schema version {SCHEMA_VERSION}")
    except BaseException as e:
        if con.in_transaction:  # SQLite may already have rolled back (a full disk, an I/O error)
            con.execute("ROLLBACK")
        if isinstance(e, SchemaError):
            raise SchemaError(
                f"{e} (the database is unchanged, still schema version {frm}; backup: {dest.name})"
            ) from None
        if isinstance(e, sqlite3.Error):
            raise SchemaError(
                f"migration to schema version {SCHEMA_VERSION} failed: {e} (the database is"
                f" unchanged, still schema version {frm}; backup: {dest.name})"
            ) from e
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
        raise SchemaError(f"database schema version {v} is not supported (expected {SCHEMA_VERSION})")
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


CHECKPOINT_MODES = ("PASSIVE", "TRUNCATE")


def checkpoint(con: sqlite3.Connection, mode: str = "PASSIVE") -> None:
    """WAL checkpoint: ``PASSIVE`` (the periodic one, every 60 s) or ``TRUNCATE`` (at
    shutdown, which also truncates the ``-wal`` file to nothing for the next start).
    Any other mode is refused rather than passed through to SQLite (DESIGN.md §4)."""
    if mode not in CHECKPOINT_MODES:
        raise ValueError(f"unknown checkpoint mode: {mode!r}")
    con.execute(f"PRAGMA wal_checkpoint({mode})")

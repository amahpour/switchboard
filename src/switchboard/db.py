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
register to sign an agent in through the browser, and says how each agent token was made; 32 (#498)
adds the avatars an admin uploads for each harness (§29.11); 33 (#488) adds the listener keys of Claude
Code's helper scripts (§49.11); 34 (#513) admits OpenCode in participants (§49.12); 35 (#518) admits it in
``agent_icons``, so an admin can upload its avatar (§29.11); 38 (#569) adds ``people.admin``,
so a person the owner chooses is an admin too (§51); 39 (#573) adds a meeting line and the
``meetings`` table, a live call transcribed into a room (§52); 40 (#544) adds the files a room
holds, the one-time grants that upload or download them, and a broker setting for how long
files are kept (§53); 41 (#593) lets a call be a file's uploader; 42 (#623) lets a call be hosted on a
person's machine, with a key per call, and stores each line it pushes with that call (§54); 43 (#623)
records when a call went live and lets an admin upload the icon of each platform a call can be on, in
``agent_icons`` (§29.11); 44 (#637) binds script keys to their harness (§49.11); 45 (#663) adds what
each person has seen of each room and which board cards they set aside, for the home page (§55.8);
46 adds room whiteboards (§56); 47 adds membership listening and its per-call wake count (§52.1);
48 adds a room's hands-free call settings (§52.3); 49 (#713) adds the voice a person pinned for a member
on calls (§52.6).
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
from typing import NamedTuple

from switchboard.models import Room, room_slug

log = logging.getLogger("switchboard.db")

SCHEMA_VERSION = 49

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
  renamed_from TEXT NOT NULL DEFAULT '[]',
  call_voice INTEGER NOT NULL DEFAULT 0 CHECK(call_voice IN (0, 1)),
  call_listen INTEGER NOT NULL DEFAULT 0 CHECK(call_listen IN (0, 1)));

CREATE TABLE participants(
  id INTEGER PRIMARY KEY,
  harness TEXT NOT NULL CHECK(harness IN ('claude','codex','cursor','devin','opencode','test','unknown')),
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
  join_code_hash TEXT,
  quiet INTEGER NOT NULL DEFAULT 0 CHECK(quiet IN (0, 1)),
  listen INTEGER NOT NULL DEFAULT 0 CHECK(listen IN (0, 1)),
  listen_call_id INTEGER REFERENCES meetings(id),
  listen_wakes INTEGER NOT NULL DEFAULT 0 CHECK(listen_wakes >= 0),
  call_voice TEXT CHECK(call_voice IS NULL OR (length(call_voice) BETWEEN 1 AND 32
    AND call_voice NOT GLOB '*[^a-z0-9_]*')));
CREATE UNIQUE INDEX memberships_active_name ON memberships(room_id, screen_name) WHERE left_at IS NULL;
CREATE UNIQUE INDEX memberships_active_part ON memberships(room_id, participant_id) WHERE left_at IS NULL;

CREATE TABLE messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL REFERENCES rooms(id), ts REAL NOT NULL,
  sender_membership_id INTEGER,
  sender_name TEXT NOT NULL, sender_harness TEXT,
  sender_kind TEXT NOT NULL CHECK(sender_kind IN ('human','agent','system','meeting')),
  via TEXT NOT NULL CHECK(via IN ('web','cli','mcp','system','meeting')),
  kind TEXT NOT NULL DEFAULT 'chat' CHECK(kind IN ('chat','join','leave','notice')),
  text TEXT NOT NULL, reply_to INTEGER, mentions TEXT NOT NULL DEFAULT '[]',
  sender_host TEXT,
  sender_person_id INTEGER,
  meeting_id INTEGER REFERENCES meetings(id));
CREATE INDEX messages_room_id ON messages(room_id, id);
CREATE INDEX messages_meeting ON messages(meeting_id) WHERE meeting_id IS NOT NULL;

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
  last_name TEXT CHECK(last_name IS NULL OR length(last_name) <= 64),
  admin INTEGER NOT NULL DEFAULT 0 CHECK(admin IN (0, 1)));
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
  note TEXT NOT NULL DEFAULT '',
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
CREATE TABLE agent_icons(
  harness TEXT NOT NULL CHECK(harness IN
    ('claude','codex','cursor','devin','opencode','zoom','meet','teams')),
  scheme TEXT NOT NULL CHECK(scheme IN ('light','dark')),
  mime TEXT NOT NULL CHECK(mime IN ('image/png','image/webp')),
  data BLOB NOT NULL CHECK(length(data) BETWEEN 1 AND 262144),
  width INTEGER NOT NULL CHECK(width BETWEEN 1 AND 512),
  height INTEGER NOT NULL CHECK(height BETWEEN 1 AND 512),
  sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
  updated_at REAL NOT NULL,
  PRIMARY KEY(harness, scheme));
CREATE TABLE listener_keys(
  id INTEGER PRIMARY KEY,
  token_id INTEGER NOT NULL REFERENCES agent_tokens(id),
  key_hash TEXT NOT NULL UNIQUE CHECK(length(key_hash) = 64),
  session_id TEXT CHECK(session_id IS NULL OR length(session_id) <= 128),
  created_at REAL NOT NULL, last_used_at REAL NOT NULL,
  harness TEXT NOT NULL DEFAULT 'claude' CHECK(harness IN ('claude','codex','unknown')));
CREATE INDEX listener_keys_token ON listener_keys(token_id, last_used_at);

CREATE TABLE meetings(
  id INTEGER PRIMARY KEY,
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  state TEXT NOT NULL CHECK(state IN ('joining','live','ended')),
  started_at REAL NOT NULL,
  ended_at REAL,
  started_by TEXT NOT NULL CHECK(length(started_by) BETWEEN 1 AND 80),
  platform TEXT NOT NULL CHECK(platform IN ('zoom','meet','teams','unknown')),
  error TEXT CHECK(error IS NULL OR length(error) <= 500),
  source TEXT NOT NULL DEFAULT 'server' CHECK(source IN ('server','hosted')),
  key_hash TEXT CHECK(key_hash IS NULL OR length(key_hash) = 64),
  token_id INTEGER REFERENCES agent_tokens(id),
  owner_person_id INTEGER,
  host TEXT CHECK(host IS NULL OR length(host) BETWEEN 1 AND 24),
  model TEXT CHECK(model IS NULL OR length(model) <= 80),
  lines_pushed INTEGER NOT NULL DEFAULT 0 CHECK(lines_pushed >= 0),
  live_at REAL);
CREATE UNIQUE INDEX meetings_one_open ON meetings(room_id) WHERE state!='ended';
CREATE UNIQUE INDEX meetings_key ON meetings(key_hash) WHERE key_hash IS NOT NULL;

CREATE TABLE broker_settings(
  key TEXT PRIMARY KEY CHECK(length(key) BETWEEN 1 AND 64),
  value TEXT NOT NULL CHECK(length(value) <= 200));

CREATE TABLE files(
  id TEXT PRIMARY KEY CHECK(length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'),
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 200),
  size INTEGER CHECK(size IS NULL OR size >= 0),
  sha256 TEXT CHECK(sha256 IS NULL OR length(sha256) = 64),
  content_type TEXT NOT NULL DEFAULT '' CHECK(length(content_type) <= 200),
  uploader_name TEXT NOT NULL CHECK(length(uploader_name) BETWEEN 1 AND 100),
  uploader_kind TEXT NOT NULL CHECK(uploader_kind IN ('human','agent','meeting')),
  message_id INTEGER REFERENCES messages(id),
  state TEXT NOT NULL CHECK(state IN ('pending','stored')),
  created_at REAL NOT NULL,
  stored_at REAL);
CREATE INDEX files_room ON files(room_id);
CREATE INDEX files_message ON files(message_id);

CREATE TABLE file_grants(
  token_hash TEXT PRIMARY KEY CHECK(length(token_hash) = 64),
  file_id TEXT NOT NULL REFERENCES files(id),
  kind TEXT NOT NULL CHECK(kind IN ('upload','download')),
  expires_at REAL NOT NULL,
  used_at REAL,
  membership_id INTEGER);
CREATE INDEX file_grants_file ON file_grants(file_id);
CREATE TABLE home_seen(
  person_key INTEGER NOT NULL CHECK(person_key >= 0),
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  seen_id INTEGER NOT NULL CHECK(seen_id >= 0),
  PRIMARY KEY(person_key, room_id)) WITHOUT ROWID;
CREATE TABLE home_snoozes(
  person_key INTEGER NOT NULL CHECK(person_key >= 0),
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  review_id INTEGER NOT NULL,
  snoozed_at REAL NOT NULL,
  PRIMARY KEY(person_key, room_id, review_id)) WITHOUT ROWID;

CREATE TABLE room_boards(
  room_id INTEGER PRIMARY KEY REFERENCES rooms(id),
  seq INTEGER NOT NULL CHECK(seq >= 0),
  elements TEXT NOT NULL CHECK(length(elements) <= 1000000),
  updated_at REAL NOT NULL);
CREATE TABLE room_board_changes(
  id INTEGER PRIMARY KEY,
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  seq INTEGER NOT NULL CHECK(seq > 0),
  actor TEXT NOT NULL CHECK(length(actor) BETWEEN 1 AND 100),
  at REAL NOT NULL,
  ids TEXT NOT NULL CHECK(length(ids) <= 8000),
  n INTEGER NOT NULL CHECK(n >= 1));
CREATE INDEX room_board_changes_room ON room_board_changes(room_id, seq);
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
V31_TABLES = (*V30_TABLES, "oauth_clients")  # before agent_icons (v32)
V32_TABLES = (*V31_TABLES, "agent_icons")  # before listener_keys (v33)
V38_TABLES = (*V32_TABLES, "listener_keys")  # schemas 33 through 38, before meetings (v39)
V39_TABLES = (*V38_TABLES, "meetings")  # before files (v40)
V44_TABLES = (*V39_TABLES, "broker_settings", "files", "file_grants")  # before home_seen (v45)
V45_TABLES = (*V44_TABLES, "home_seen", "home_snoozes")  # before the whiteboard (v46)
TABLES = (*V45_TABLES, "room_boards", "room_board_changes")

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


# v31 -> v32 (#498, DESIGN.md §29.11): the avatars an admin uploads, one image per harness and scheme, kept
# in the database so they are in every backup and need no volume. New, so empty. The CHECKs are the last
# line of the broker's own validation: only the four harnesses with an avatar, the two schemes, PNG or
# WebP (never SVG) and the byte and pixel caps.
V31_TO_V32 = (
    "CREATE TABLE agent_icons("
    " harness TEXT NOT NULL CHECK(harness IN ('claude','codex','cursor','devin')),"
    " scheme TEXT NOT NULL CHECK(scheme IN ('light','dark')),"
    " mime TEXT NOT NULL CHECK(mime IN ('image/png','image/webp')),"
    " data BLOB NOT NULL CHECK(length(data) BETWEEN 1 AND 262144),"
    " width INTEGER NOT NULL CHECK(width BETWEEN 1 AND 512),"
    " height INTEGER NOT NULL CHECK(height BETWEEN 1 AND 512),"
    " sha256 TEXT NOT NULL CHECK(length(sha256) = 64),"
    " updated_at REAL NOT NULL,"
    " PRIMARY KEY(harness, scheme))",
    "UPDATE meta SET value='32' WHERE key='schema_version'",
)


# v32 -> v33 (#488, DESIGN.md §49.11): the listener keys of Claude Code's helper scripts. A key is made on
# the machine and claimed over a signed-in connection; only its sha256 is kept, with the token that
# claimed it and the one session it is for. New, so empty.
V32_TO_V33 = (
    "CREATE TABLE listener_keys("
    " id INTEGER PRIMARY KEY,"
    " token_id INTEGER NOT NULL REFERENCES agent_tokens(id),"
    " key_hash TEXT NOT NULL UNIQUE CHECK(length(key_hash) = 64),"
    " session_id TEXT CHECK(session_id IS NULL OR length(session_id) <= 128),"
    " created_at REAL NOT NULL, last_used_at REAL NOT NULL)",
    "CREATE INDEX listener_keys_token ON listener_keys(token_id, last_used_at)",
    "UPDATE meta SET value='33' WHERE key='schema_version'",
)


# v33 -> v34 (#513, §49.12): only the participants CHECK gains a harness. SQLite's documented
# schema-text procedure for a CHECK change leaves rows, indexes and incoming foreign keys intact
# (https://www.sqlite.org/lang_altertable.html#otheralter). _migrate brackets this one step with
# schema editing, advances SQLite's schema cookie, reloads the schema and checks integrity before commit.
V33_TO_V34 = (
    "UPDATE sqlite_master SET sql=replace(sql,"
    " '''claude'',''codex'',''cursor'',''devin'',''test'',''unknown''',"
    " '''claude'',''codex'',''cursor'',''devin'',''opencode'',''test'',''unknown''')"
    " WHERE type='table' AND name='participants'",
    "UPDATE meta SET value='34' WHERE key='schema_version'",
)


# v34 -> v35 (#518, §29.11): only the agent_icons CHECK gains a harness, so an admin can upload OpenCode's
# avatar. The same schema-text edit as v33 -> v34, with the same checks: _migrate brackets this step with
# schema editing, verifies the new CHECK is in the table's text (or fails closed and rolls back), advances
# SQLite's schema cookie and checks integrity. Rows are untouched. The needle ends at the closing
# parenthesis, so it can only match the CHECK on harness.
V34_TO_V35 = (
    "UPDATE sqlite_master SET sql=replace(sql,"
    " '''claude'',''codex'',''cursor'',''devin'')',"
    " '''claude'',''codex'',''cursor'',''devin'',''opencode'')')"
    " WHERE type='table' AND name='agent_icons'",
    "UPDATE meta SET value='35' WHERE key='schema_version'",
)

# v35 -> v36 (#548, DESIGN.md §50): a membership can be quiet, so unaddressed chatter never starts
# a turn for that agent. Every existing membership stays off (0): delivery is unchanged until a
# person turns it on. NOT NULL and only 0 or 1, so there is no unknown mode to treat as on.
V35_TO_V36 = (
    "ALTER TABLE memberships ADD COLUMN quiet INTEGER NOT NULL DEFAULT 0 CHECK(quiet IN (0, 1))",
    "UPDATE meta SET value='36' WHERE key='schema_version'",
)

# v36 -> v37 (#545, DESIGN.md §47): a claim can carry a note, its state or a link. Every
# existing claim gets '': a missing note is empty, not an unknown value. NOT NULL so a
# later read never has to treat NULL as a note.
V36_TO_V37 = (
    "ALTER TABLE claims ADD COLUMN note TEXT NOT NULL DEFAULT ''",
    "UPDATE meta SET value='37' WHERE key='schema_version'",
)

# v37 -> v38 (#569, DESIGN.md §51): a person can be an admin. Every existing person stays
# off (0), so only the owner is an admin until one is made. NOT NULL and only 0 or 1, so
# there is no unknown flag to treat as on. The owner has no people row (§32.2).
V37_TO_V38 = (
    "ALTER TABLE people ADD COLUMN admin INTEGER NOT NULL DEFAULT 0 CHECK(admin IN (0, 1))",
    "UPDATE meta SET value='38' WHERE key='schema_version'",
)

# v38 -> v39 (#573, DESIGN.md §52): a meeting line, and one row per call. The CHECKs gain
# 'meeting' by the same schema-text edit as v33 -> v34 (rows, indexes and foreign keys stay).
# The meetings table is new, so empty. One open call per room: the partial unique index.
V38_TO_V39 = (
    "UPDATE sqlite_master SET sql=replace(sql,"
    " '''human'',''agent'',''system''',"
    " '''human'',''agent'',''system'',''meeting''')"
    " WHERE type='table' AND name='messages'",
    "UPDATE sqlite_master SET sql=replace(sql,"
    " '''web'',''cli'',''mcp'',''system''',"
    " '''web'',''cli'',''mcp'',''system'',''meeting''')"
    " WHERE type='table' AND name='messages'",
    "CREATE TABLE meetings("
    " id INTEGER PRIMARY KEY,"
    " room_id INTEGER NOT NULL REFERENCES rooms(id),"
    " state TEXT NOT NULL CHECK(state IN ('joining','live','ended')),"
    " started_at REAL NOT NULL,"
    " ended_at REAL,"
    " started_by TEXT NOT NULL CHECK(length(started_by) BETWEEN 1 AND 80),"
    " platform TEXT NOT NULL CHECK(platform IN ('zoom','meet','teams')),"
    " error TEXT CHECK(error IS NULL OR length(error) <= 500))",
    "CREATE UNIQUE INDEX meetings_one_open ON meetings(room_id) WHERE state!='ended'",
    "UPDATE meta SET value='39' WHERE key='schema_version'",
)

# v39 -> v40 (#544, DESIGN.md §53): one row per file a room holds, the grants that upload or
# download it (the sha256 of a token, never the token), and a small table of broker settings
# (how long files are kept). Empty on an existing database. The bytes are not in SQLite.
V39_TO_V40 = (
    "CREATE TABLE broker_settings("
    " key TEXT PRIMARY KEY CHECK(length(key) BETWEEN 1 AND 64),"
    " value TEXT NOT NULL CHECK(length(value) <= 200))",
    "CREATE TABLE files("
    " id TEXT PRIMARY KEY CHECK(length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'),"
    " room_id INTEGER NOT NULL REFERENCES rooms(id),"
    " name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 200),"
    " size INTEGER CHECK(size IS NULL OR size >= 0),"
    " sha256 TEXT CHECK(sha256 IS NULL OR length(sha256) = 64),"
    " content_type TEXT NOT NULL DEFAULT '' CHECK(length(content_type) <= 200),"
    " uploader_name TEXT NOT NULL CHECK(length(uploader_name) BETWEEN 1 AND 100),"
    " uploader_kind TEXT NOT NULL CHECK(uploader_kind IN ('human','agent')),"
    " message_id INTEGER REFERENCES messages(id),"
    " state TEXT NOT NULL CHECK(state IN ('pending','stored')),"
    " created_at REAL NOT NULL,"
    " stored_at REAL)",
    "CREATE INDEX files_room ON files(room_id)",
    "CREATE INDEX files_message ON files(message_id)",
    "CREATE TABLE file_grants("
    " token_hash TEXT PRIMARY KEY CHECK(length(token_hash) = 64),"
    " file_id TEXT NOT NULL REFERENCES files(id),"
    " kind TEXT NOT NULL CHECK(kind IN ('upload','download')),"
    " expires_at REAL NOT NULL,"
    " used_at REAL,"
    " membership_id INTEGER)",
    "CREATE INDEX file_grants_file ON file_grants(file_id)",
    "UPDATE meta SET value='40' WHERE key='schema_version'",
)

# v40 -> v41 (#593, DESIGN.md §52): a snapshot of a call's shared screen is a room file whose uploader is
# the call itself, when a line on the call asked for it, so the files CHECK gains 'meeting'. The same
# schema-text edit as v38 -> v39, on the one table: rows, indexes and foreign keys stay. _migrate brackets
# it with schema editing and fails closed (rolls back) if the new CHECK is not in the table's text.
V40_TO_V41 = (
    "UPDATE sqlite_master SET sql=replace(sql,"
    " '''human'',''agent'')',"
    " '''human'',''agent'',''meeting'')')"
    " WHERE type='table' AND name='files'",
    "UPDATE meta SET value='41' WHERE key='schema_version'",
)


# v41 -> v42 (#623, DESIGN.md §54): a call can be hosted on a person's machine, which pushes its lines in
# with a key made for that one call. Every existing call is the server's own (``source`` 'server'), with no
# key, token or host. The columns are added the usual way (rows are untouched, the new ones read their
# default). ``platform`` also admits 'unknown', for a hosted call that doesn't say which platform it is on:
# the same schema-text edit as v38 -> v39, on the one table, which _migrate brackets with schema editing and
# fails closed (rolls back) if the new CHECK is not in the table's text. The key's sha256 is unique, so a
# key cannot be registered for two calls, and a line pushed by a hosted call is stored with its call
# (``messages.meeting_id``): who pushed what stays on record after the call ends.
V41_TO_V42 = (
    "ALTER TABLE meetings ADD COLUMN source TEXT NOT NULL DEFAULT 'server'"
    " CHECK(source IN ('server','hosted'))",
    "ALTER TABLE meetings ADD COLUMN key_hash TEXT CHECK(key_hash IS NULL OR length(key_hash) = 64)",
    "ALTER TABLE meetings ADD COLUMN token_id INTEGER REFERENCES agent_tokens(id)",
    "ALTER TABLE meetings ADD COLUMN owner_person_id INTEGER",
    "ALTER TABLE meetings ADD COLUMN host TEXT CHECK(host IS NULL OR length(host) BETWEEN 1 AND 24)",
    "ALTER TABLE meetings ADD COLUMN model TEXT CHECK(model IS NULL OR length(model) <= 80)",
    "ALTER TABLE meetings ADD COLUMN lines_pushed INTEGER NOT NULL DEFAULT 0 CHECK(lines_pushed >= 0)",
    "ALTER TABLE messages ADD COLUMN meeting_id INTEGER REFERENCES meetings(id)",
    "CREATE UNIQUE INDEX meetings_key ON meetings(key_hash) WHERE key_hash IS NOT NULL",
    "CREATE INDEX messages_meeting ON messages(meeting_id) WHERE meeting_id IS NOT NULL",
    "UPDATE sqlite_master SET sql=replace(sql,"
    " '''zoom'',''meet'',''teams'')',"
    " '''zoom'',''meet'',''teams'',''unknown'')')"
    " WHERE type='table' AND name='meetings'",
    "UPDATE meta SET value='42' WHERE key='schema_version'",
)


# v42 -> v43 (#623, DESIGN.md §29.11, §54): a call's details say when it went live, and an admin can
# upload the icon of each platform a call can be on. ``meetings.live_at`` is NULL for a call that was live
# before this step (the page then has nothing to say about when). The icons go in ``agent_icons`` with the
# harnesses' (a platform is one more name its CHECK admits, 'zoom', 'meet' and 'teams'): the same
# schema-text edit as v34 -> v35, which _migrate brackets with schema editing, verifies (the new CHECK must
# be in the table's text, or the step fails closed and rolls back), and follows with the integrity checks.
# Rows are untouched. The needle ends at the closing parenthesis, so it can only match the CHECK on harness.
V42_TO_V43 = (
    "ALTER TABLE meetings ADD COLUMN live_at REAL",
    "UPDATE sqlite_master SET sql=replace(sql,"
    " '''devin'',''opencode'')',"
    " '''devin'',''opencode'',''zoom'',''meet'',''teams'')')"
    " WHERE type='table' AND name='agent_icons'",
    "UPDATE meta SET value='43' WHERE key='schema_version'",
)


# v43 -> v44 (#637, DESIGN.md §49.11): old keys remain Claude's. A Codex key is pinned to
# both its harness and its first authenticated thread, never a same-id session of another harness.
V43_TO_V44 = (
    "ALTER TABLE listener_keys ADD COLUMN harness TEXT NOT NULL DEFAULT 'claude'"
    " CHECK(harness IN ('claude','codex','unknown'))",
    "UPDATE meta SET value='44' WHERE key='schema_version'",
)

# v44 -> v45 (#663, DESIGN.md §55.8): the home page remembers, for each person, how far they have read each
# room (``home_seen``: the newest message id they were shown, which only moves forward) and which review
# boards they set aside with "Not now" (``home_snoozes``: when they pressed it; a board that changes after
# that comes back). ``person_key`` is ``room_people``'s: 0 for the owner. Both are new, so empty, and both
# follow a deleted room (§28.6).
V44_TO_V45 = (
    "CREATE TABLE home_seen("
    " person_key INTEGER NOT NULL CHECK(person_key >= 0),"
    " room_id INTEGER NOT NULL REFERENCES rooms(id),"
    " seen_id INTEGER NOT NULL CHECK(seen_id >= 0),"
    " PRIMARY KEY(person_key, room_id)) WITHOUT ROWID",
    "CREATE TABLE home_snoozes("
    " person_key INTEGER NOT NULL CHECK(person_key >= 0),"
    " room_id INTEGER NOT NULL REFERENCES rooms(id),"
    " review_id INTEGER NOT NULL,"
    " snoozed_at REAL NOT NULL,"
    " PRIMARY KEY(person_key, room_id, review_id)) WITHOUT ROWID",
    "UPDATE meta SET value='45' WHERE key='schema_version'",
)


# v45 -> v46 (#656, DESIGN.md §56): one whiteboard per room, and a short record of each accepted
# change (who, when, which ids). The elements are one JSON document; the change rows are not copies of it.
V45_TO_V46 = (
    "CREATE TABLE room_boards("
    " room_id INTEGER PRIMARY KEY REFERENCES rooms(id),"
    " seq INTEGER NOT NULL CHECK(seq >= 0),"
    " elements TEXT NOT NULL CHECK(length(elements) <= 1000000),"
    " updated_at REAL NOT NULL)",
    "CREATE TABLE room_board_changes("
    " id INTEGER PRIMARY KEY,"
    " room_id INTEGER NOT NULL REFERENCES rooms(id),"
    " seq INTEGER NOT NULL CHECK(seq > 0),"
    " actor TEXT NOT NULL CHECK(length(actor) BETWEEN 1 AND 100),"
    " at REAL NOT NULL,"
    " ids TEXT NOT NULL CHECK(length(ids) <= 8000),"
    " n INTEGER NOT NULL CHECK(n >= 1))",
    "CREATE INDEX room_board_changes_room ON room_board_changes(room_id, seq)",
    "UPDATE meta SET value='46' WHERE key='schema_version'",
)


# v46 -> v47 (#697, DESIGN.md §52.1): listening and its per-call wake counter survive toggles.
V46_TO_V47 = (
    "ALTER TABLE memberships ADD COLUMN listen INTEGER NOT NULL DEFAULT 0 CHECK(listen IN (0, 1))",
    "ALTER TABLE memberships ADD COLUMN listen_call_id INTEGER REFERENCES meetings(id)",
    "ALTER TABLE memberships ADD COLUMN listen_wakes INTEGER NOT NULL DEFAULT 0 CHECK(listen_wakes >= 0)",
    "UPDATE meta SET value='47' WHERE key='schema_version'",
)


# v47 -> v48 (#697, DESIGN.md §52.3): a room's hands-free call settings, kept with or without a live call.
V47_TO_V48 = (
    "ALTER TABLE rooms ADD COLUMN call_voice INTEGER NOT NULL DEFAULT 0 CHECK(call_voice IN (0, 1))",
    "ALTER TABLE rooms ADD COLUMN call_listen INTEGER NOT NULL DEFAULT 0 CHECK(call_listen IN (0, 1))",
    "UPDATE meta SET value='48' WHERE key='schema_version'",
)


# v48 -> v49 (#713, DESIGN.md §52.6): the Kokoro voice a person pinned for a member on calls, by
# `/meeting voice <name> <voice>`. NULL: the default from the member's screen name. The shape is checked here
# as well as where it is set.
V48_TO_V49 = (
    "ALTER TABLE memberships ADD COLUMN call_voice TEXT CHECK(call_voice IS NULL OR"
    " (length(call_voice) BETWEEN 1 AND 32 AND call_voice NOT GLOB '*[^a-z0-9_]*'))",
    "UPDATE meta SET value='49' WHERE key='schema_version'",
)


class MigrationStep(NamedTuple):
    to: int
    statements: tuple[str, ...]
    new_tables: tuple[str, ...]
    # Nonempty enables writable_schema for this step and verifies each required SQL fragment.
    schema_edits: tuple[tuple[str, str], ...] = ()


def _steps(frm: int) -> list[MigrationStep]:
    """Migration statements, new tables and schema-edit checks, read at run time, never cached."""
    all_steps = {
        1: MigrationStep(2, V1_TO_V2, ("remotes",)),
        2: MigrationStep(3, V2_TO_V3, ("passkeys", "link_machines")),
        3: MigrationStep(4, V3_TO_V4, ("people",)),
        4: MigrationStep(5, V4_TO_V5, ("preferences",)),
        5: MigrationStep(6, V5_TO_V6, ()),
        6: MigrationStep(7, V6_TO_V7, ()),
        7: MigrationStep(8, V7_TO_V8, ()),
        8: MigrationStep(9, V8_TO_V9, ("reviews", "review_items")),
        9: MigrationStep(10, V9_TO_V10, ()),
        10: MigrationStep(11, V10_TO_V11, ()),
        11: MigrationStep(12, V11_TO_V12, ()),
        12: MigrationStep(13, V12_TO_V13, ()),
        13: MigrationStep(14, V13_TO_V14, ()),
        14: MigrationStep(15, V14_TO_V15, ("room_people",)),
        15: MigrationStep(16, V15_TO_V16, ()),
        16: MigrationStep(17, V16_TO_V17, ()),
        17: MigrationStep(18, V17_TO_V18, ()),
        18: MigrationStep(19, V18_TO_V19, ()),
        19: MigrationStep(20, V19_TO_V20, ()),
        20: MigrationStep(21, V20_TO_V21, ("claims",)),
        21: MigrationStep(22, V21_TO_V22, ("pins",)),
        22: MigrationStep(23, V22_TO_V23, ()),
        23: MigrationStep(24, V23_TO_V24, ()),
        24: MigrationStep(25, V24_TO_V25, ("agent_tokens",)),
        25: MigrationStep(26, V25_TO_V26, ("review_picks",)),
        26: MigrationStep(27, V26_TO_V27, ()),
        27: MigrationStep(28, V27_TO_V28, ()),
        28: MigrationStep(29, V28_TO_V29, ()),
        29: MigrationStep(30, V29_TO_V30, ()),
        30: MigrationStep(31, V30_TO_V31, ("oauth_clients",)),
        31: MigrationStep(32, V31_TO_V32, ("agent_icons",)),
        32: MigrationStep(33, V32_TO_V33, ("listener_keys",)),
        33: MigrationStep(34, V33_TO_V34, (), (("participants", "'opencode'"),)),
        34: MigrationStep(35, V34_TO_V35, (), (("agent_icons", "'devin','opencode'"),)),
        35: MigrationStep(36, V35_TO_V36, ()),
        36: MigrationStep(37, V36_TO_V37, ()),
        37: MigrationStep(38, V37_TO_V38, ()),
        38: MigrationStep(
            39,
            V38_TO_V39,
            ("meetings",),
            (
                ("messages", "'human','agent','system','meeting'"),
                ("messages", "'web','cli','mcp','system','meeting'"),
            ),
        ),
        39: MigrationStep(40, V39_TO_V40, ("broker_settings", "files", "file_grants")),
        40: MigrationStep(41, V40_TO_V41, (), (("files", "'human','agent','meeting')"),)),
        41: MigrationStep(42, V41_TO_V42, (), (("meetings", "'zoom','meet','teams','unknown')"),)),
        42: MigrationStep(43, V42_TO_V43, (), (("agent_icons", "'opencode','zoom','meet','teams')"),)),
        43: MigrationStep(44, V43_TO_V44, ()),
        44: MigrationStep(45, V44_TO_V45, ("home_seen", "home_snoozes")),
        45: MigrationStep(46, V45_TO_V46, ("room_boards", "room_board_changes")),
        46: MigrationStep(47, V46_TO_V47, ()),
        47: MigrationStep(48, V47_TO_V48, ()),
        48: MigrationStep(49, V48_TO_V49, ()),
    }
    out = []
    v = frm
    while v < SCHEMA_VERSION:
        step = all_steps[v]
        out.append(step)
        v = step.to
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
    if version <= 31:
        return V31_TABLES
    if version <= 32:
        return V32_TABLES
    if version <= 38:
        return V38_TABLES
    if version == 39:
        return V39_TABLES
    if version <= 44:
        return V44_TABLES
    if version == 45:
        return V45_TABLES
        return V44_TABLES
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


@contextmanager
def read_snapshot(con: sqlite3.Connection) -> Iterator[None]:
    """Pin a deferred WAL read snapshot for a live archive's counts and backup (DESIGN.md §57)."""
    con.execute("BEGIN")
    try:
        schema_version(con)  # the first read pins the snapshot
        yield
    finally:
        con.execute("ROLLBACK")


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
        for to, stmts, new, schema_edits in _steps(frm):
            if schema_edits:
                cookie = int(con.execute("PRAGMA schema_version").fetchone()[0])
                con.execute("PRAGMA writable_schema=ON")
            try:
                for stmt in stmts:
                    con.execute(stmt)
                for table, required in schema_edits:
                    row = con.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()
                    if row is None or required not in row[0]:
                        raise SchemaError(f"schema edit of {table} did not add {required}")
                if schema_edits:
                    con.execute(f"PRAGMA schema_version={cookie + 1}")
            finally:
                if schema_edits:
                    con.execute("PRAGMA writable_schema=RESET")
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

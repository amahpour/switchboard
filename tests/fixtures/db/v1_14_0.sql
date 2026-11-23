BEGIN TRANSACTION;
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
CREATE TABLE agent_tokens(
  id INTEGER PRIMARY KEY, person_id INTEGER,
  device TEXT NOT NULL CHECK(length(device) BETWEEN 1 AND 24),
  token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
  created_at REAL NOT NULL, last_used_at REAL, revoked_at REAL,
  via TEXT NOT NULL DEFAULT 'token' CHECK(via IN ('token', 'oauth')),
  client_name TEXT CHECK(client_name IS NULL OR length(client_name) <= 80));
INSERT INTO "agent_tokens" VALUES(1,NULL,'laptop','b194f3d4502c2133667d602cf123a35a8d8efaeec2705c04c68d3d4eefa1efbf',1790000000.0,NULL,NULL,'token',NULL);
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
CREATE TABLE broker_settings(
  key TEXT PRIMARY KEY CHECK(length(key) BETWEEN 1 AND 64),
  value TEXT NOT NULL CHECK(length(value) <= 200));
CREATE TABLE claims(
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  key_norm TEXT NOT NULL CHECK(length(key_norm) BETWEEN 1 AND 512),
  key TEXT NOT NULL CHECK(length(key) BETWEEN 1 AND 120),
  membership_id INTEGER NOT NULL REFERENCES memberships(id),
  expires_at REAL NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(room_id, key_norm)) WITHOUT ROWID;
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
INSERT INTO "deliveries" VALUES(1,1,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(2,1,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(2,2,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(3,1,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(3,2,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(4,1,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(4,2,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(5,1,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(5,2,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(6,1,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(6,2,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(7,1,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(7,2,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, room_id INTEGER,
  membership_id INTEGER, participant_id INTEGER, kind TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE file_grants(
  token_hash TEXT PRIMARY KEY CHECK(length(token_hash) = 64),
  file_id TEXT NOT NULL REFERENCES files(id),
  kind TEXT NOT NULL CHECK(kind IN ('upload','download')),
  expires_at REAL NOT NULL,
  used_at REAL,
  membership_id INTEGER);
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
CREATE TABLE listener_keys(
  id INTEGER PRIMARY KEY,
  token_id INTEGER NOT NULL REFERENCES agent_tokens(id),
  key_hash TEXT NOT NULL UNIQUE CHECK(length(key_hash) = 64),
  session_id TEXT CHECK(session_id IS NULL OR length(session_id) <= 128),
  created_at REAL NOT NULL, last_used_at REAL NOT NULL,
  harness TEXT NOT NULL DEFAULT 'claude' CHECK(harness IN ('claude','codex','unknown')));
CREATE TABLE mcp_sessions( token_id INTEGER NOT NULL REFERENCES agent_tokens(id), sid_hash TEXT NOT NULL CHECK(length(sid_hash) = 64), client_name TEXT NOT NULL CHECK(length(client_name) BETWEEN 1 AND 80), client_version TEXT NOT NULL CHECK(length(client_version) <= 80), protocol TEXT NOT NULL CHECK(length(protocol) BETWEEN 1 AND 32), opened_at REAL NOT NULL, PRIMARY KEY(token_id, sid_hash)) WITHOUT ROWID;
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
INSERT INTO "memberships" VALUES(1,1,1,'claude-1',1790000001.0,0,NULL,NULL,0,0,NULL,0,-1,0,'f1f87e0d0d9a129b63f7f58fe3b8de2ce36c151e9dc20b84f9203c27f3b46540',0,0,NULL,0,NULL);
INSERT INTO "memberships" VALUES(2,1,2,'codex-1',1790000001.5,0,NULL,NULL,0,0,NULL,0,-1,0,'693953764a01ed4dc0d5be8dc77bc5460a7d8941752ec29fabf88a65a7170850',0,0,NULL,0,NULL);
INSERT INTO "memberships" VALUES(3,1,3,'cursor-1',1790000002.0,0,NULL,NULL,0,0,NULL,0,-1,0,'82fa698d9e738c47dba1082f83b38899089b9a4cc1d131ec6af2b6dca87674b6',0,0,NULL,0,NULL);
INSERT INTO "memberships" VALUES(4,1,4,'devin-1',1790000002.5,0,NULL,NULL,0,0,NULL,0,-1,0,'00990c2f48d3afb9d4e1bdbe2867c5da9f49e9da317ee3993d77092e9d2fdcb2',0,0,NULL,0,NULL);
INSERT INTO "memberships" VALUES(5,1,5,'opencode-1',1790000003.0,0,NULL,NULL,0,0,NULL,0,-1,0,'42b993f75d92373317206b3174d2db75cfc554889edb131786eb8969c496a9bb',0,0,NULL,0,NULL);
INSERT INTO "memberships" VALUES(6,1,6,'test-1',1790000003.5,0,NULL,NULL,0,0,NULL,0,-1,0,'5613647cdc2e4b83ccd58ae40ce9d1778a1e424f5f8f2456388cd910902a7f33',0,0,NULL,0,NULL);
INSERT INTO "memberships" VALUES(7,1,7,'unknown-1',1790000004.0,0,NULL,NULL,0,0,NULL,0,-1,0,'4a3dee95311053594c843770191162d9233263efdf3446ea778207d0ff66b08d',0,0,NULL,0,NULL);
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
INSERT INTO "messages" VALUES(1,1,1790000004.5,NULL,'alice',NULL,'human','web','chat','@claude-1 run the tests',NULL,'[]',NULL,NULL,NULL);
INSERT INTO "messages" VALUES(2,1,1790000005.5,1,'claude-1','claude','agent','mcp','chat','all green',NULL,'[]',NULL,NULL,NULL);
PRAGMA writable_schema=ON;
INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql)VALUES('table','messages_fts','messages_fts',0,'CREATE VIRTUAL TABLE messages_fts USING fts5(text, content=''search_source'', content_rowid=''id'', tokenize=''unicode61 remove_diacritics 2'')');
CREATE TABLE 'messages_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
INSERT INTO "messages_fts_config" VALUES('version',4);
CREATE TABLE 'messages_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
INSERT INTO "messages_fts_data" VALUES(1,X'0207');
INSERT INTO "messages_fts_data" VALUES(10,X'000000000102020002010101020101');
INSERT INTO "messages_fts_data" VALUES(137438953473,X'0000002E0230310102030106636C61756465010202010372756E010204010574657374730102060202686501020504060B080A');
INSERT INTO "messages_fts_data" VALUES(274877906945,X'000000160430616C6C0202020105677265656E0202030408');
CREATE TABLE 'messages_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
INSERT INTO "messages_fts_docsize" VALUES(1,X'05');
INSERT INTO "messages_fts_docsize" VALUES(2,X'02');
CREATE TABLE 'messages_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
INSERT INTO "messages_fts_idx" VALUES(1,X'',2);
INSERT INTO "messages_fts_idx" VALUES(2,X'',2);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "meta" VALUES('schema_version','60');
CREATE TABLE oauth_clients(
  client_id TEXT PRIMARY KEY CHECK(length(client_id) BETWEEN 8 AND 64),
  client_name TEXT NOT NULL CHECK(length(client_name) BETWEEN 1 AND 80),
  redirect_uris TEXT NOT NULL CHECK(length(redirect_uris) <= 2000),
  created_at REAL NOT NULL);
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
INSERT INTO "participants" VALUES(1,'claude','claude@laptop:00000000-0000-4000-8000-000000000000','00000000-0000-4000-8000-000000000000','idle',1790000001.0,'hook','claude:https',NULL,'prompting',NULL,0,NULL,0,0,1790000001.0,NULL,1790000001.0,1790000001.0,NULL,'laptop',NULL,NULL);
INSERT INTO "participants" VALUES(2,'codex','codex@laptop:00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000001','idle',1790000001.5,'hook','codex:https',NULL,'prompting',NULL,0,NULL,0,0,1790000001.5,NULL,1790000001.5,1790000001.5,NULL,'laptop',NULL,NULL);
INSERT INTO "participants" VALUES(3,'cursor','cursor@laptop:00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000002','idle',1790000002.0,'hook','cursor:https',NULL,'prompting',NULL,0,NULL,0,0,1790000002.0,NULL,1790000002.0,1790000002.0,NULL,'laptop',NULL,NULL);
INSERT INTO "participants" VALUES(4,'devin','devin@laptop:00000000-0000-4000-8000-000000000003','00000000-0000-4000-8000-000000000003','idle',1790000002.5,'hook','devin:https',NULL,'prompting',NULL,0,NULL,0,0,1790000002.5,NULL,1790000002.5,1790000002.5,NULL,'laptop',NULL,NULL);
INSERT INTO "participants" VALUES(5,'opencode','opencode@laptop:00000000-0000-4000-8000-000000000004','00000000-0000-4000-8000-000000000004','idle',1790000003.0,'hook','opencode:https',NULL,'prompting',NULL,0,NULL,0,0,1790000003.0,NULL,1790000003.0,1790000003.0,NULL,'laptop',NULL,NULL);
INSERT INTO "participants" VALUES(6,'test','test@laptop:00000000-0000-4000-8000-000000000005','00000000-0000-4000-8000-000000000005','idle',1790000003.5,'hook','test:https',NULL,'prompting',NULL,0,NULL,0,0,1790000003.5,NULL,1790000003.5,1790000003.5,NULL,'laptop',NULL,NULL);
INSERT INTO "participants" VALUES(7,'unknown','unknown@laptop:00000000-0000-4000-8000-000000000006','00000000-0000-4000-8000-000000000006','idle',1790000004.0,'hook','unknown:https',NULL,'prompting',NULL,0,NULL,0,0,1790000004.0,NULL,1790000004.0,1790000004.0,NULL,'laptop',NULL,NULL);
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
INSERT INTO "people" VALUES(1,'bob',X'01010101010101010101010101010101','e7a4eae34c12ab2c05187399d2955aefc791814675e763426370bceb05dbab4f',1,1790003600.0,1790000000.0,NULL,NULL,NULL,NULL,0);
CREATE TABLE pins(
  room_id INTEGER NOT NULL REFERENCES rooms(id), message_id INTEGER NOT NULL REFERENCES messages(id),
  pinned_by TEXT NOT NULL CHECK(length(pinned_by) <= 100), pinned_at REAL NOT NULL,
  PRIMARY KEY(room_id, message_id));
CREATE TABLE preferences(
  person_id INTEGER PRIMARY KEY CHECK(person_id >= 0),
  theme TEXT NOT NULL DEFAULT 'system' CHECK(theme IN ('system','light','dark')),
  text_size TEXT NOT NULL DEFAULT 'default' CHECK(text_size IN ('small','default','large','larger')),
  room_rules TEXT NOT NULL DEFAULT '' CHECK(length(room_rules) <= 2000),
  budget_per_hour INTEGER CHECK(budget_per_hour IS NULL OR (budget_per_hour BETWEEN 0 AND 1000000)),
  hop_limit INTEGER CHECK(hop_limit IS NULL OR (hop_limit BETWEEN 0 AND 1000)));
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
CREATE TABLE reviews(
  id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
  url TEXT NOT NULL CHECK(length(url) <= 500), head TEXT NOT NULL DEFAULT '' CHECK(length(head) <= 64),
  opened_by TEXT NOT NULL, opened_at REAL NOT NULL, closed_at REAL, posted_at REAL, posted_by TEXT,
  pr_state TEXT NOT NULL DEFAULT '' CHECK(pr_state IN ('', 'open', 'merged', 'closed')),
  pr_threads INTEGER CHECK(pr_threads IS NULL OR pr_threads BETWEEN 0 AND 999),
  pr_ci TEXT NOT NULL DEFAULT '' CHECK(pr_ci IN ('', 'passing', 'failing', 'running', 'none', 'blocked')),
  reported_by TEXT NOT NULL DEFAULT '' CHECK(length(reported_by) <= 100), reported_at REAL,
  closed_by TEXT NOT NULL DEFAULT '' CHECK(length(closed_by) <= 100),
  reopened_by TEXT NOT NULL DEFAULT '' CHECK(length(reopened_by) <= 100), reopened_at REAL);
CREATE TABLE room_board_changes(
  id INTEGER PRIMARY KEY,
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  seq INTEGER NOT NULL CHECK(seq > 0),
  actor TEXT NOT NULL CHECK(length(actor) BETWEEN 1 AND 100),
  at REAL NOT NULL,
  ids TEXT NOT NULL CHECK(length(ids) <= 8000),
  n INTEGER NOT NULL CHECK(n >= 1));
CREATE TABLE room_boards(
  room_id INTEGER PRIMARY KEY REFERENCES rooms(id),
  seq INTEGER NOT NULL CHECK(seq >= 0),
  elements TEXT NOT NULL CHECK(length(elements) <= 1000000),
  updated_at REAL NOT NULL);
CREATE TABLE room_people(
  room_id INTEGER NOT NULL REFERENCES rooms(id), person_id INTEGER NOT NULL CHECK(person_id >= 0),
  added_by INTEGER NOT NULL CHECK(added_by >= 0), added_at REAL NOT NULL,
  joined_with TEXT CHECK(joined_with IS NULL OR length(joined_with) BETWEEN 1 AND 64),
  PRIMARY KEY(room_id, person_id));
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
  call_listen INTEGER NOT NULL DEFAULT 0 CHECK(call_listen IN (0, 1)),
  review_on_link INTEGER NOT NULL DEFAULT 0 CHECK(review_on_link IN (0, 1)));
INSERT INTO "rooms" VALUES(1,'#build',1790000000.0,'alice',0,NULL,60,60,1790000000.0,NULL,1,15,1790000005.5,'',1,0,'[]',0,0,0);
CREATE TABLE say_keys( membership_id INTEGER NOT NULL REFERENCES memberships(id), key_hash TEXT NOT NULL CHECK(length(key_hash) = 64), message_id INTEGER NOT NULL REFERENCES messages(id), expires_at REAL NOT NULL, PRIMARY KEY(membership_id, key_hash)) WITHOUT ROWID;
CREATE TABLE web_sessions(id_hash TEXT PRIMARY KEY, created_at REAL NOT NULL,
  last_seen REAL NOT NULL, expires_at REAL NOT NULL,
  via TEXT,
  person_id INTEGER);
CREATE TABLE webhook_sent( webhook INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE, lane TEXT NOT NULL CHECK(lane IN ('first','second')), key TEXT NOT NULL CHECK(length(key) BETWEEN 1 AND 1000), at REAL NOT NULL, PRIMARY KEY(webhook, lane, key)) WITHOUT ROWID;
INSERT INTO "webhook_sent" VALUES(1,'first','q:build:2',1790000005.5);
CREATE TABLE webhooks( id INTEGER PRIMARY KEY AUTOINCREMENT, person INTEGER UNIQUE NOT NULL CHECK(person >= 0), url TEXT NOT NULL CHECK(length(url) BETWEEN 9 AND 500 AND lower(substr(url, 1, 8)) = 'https://'), wait_s REAL NOT NULL DEFAULT 300 CHECK(wait_s BETWEEN 60 AND 7200), second_url TEXT CHECK(second_url IS NULL OR (length(second_url) BETWEEN 9 AND 500 AND lower(substr(second_url, 1, 8)) = 'https://')), second_after_s REAL CHECK(second_after_s IS NULL OR second_after_s BETWEEN 300 AND 14400), quiet_from TEXT CHECK(quiet_from IS NULL OR quiet_from GLOB '[01][0-9]:[0-5][0-9]' OR quiet_from GLOB '2[0-3]:[0-5][0-9]'), quiet_to TEXT CHECK(quiet_to IS NULL OR quiet_to GLOB '[01][0-9]:[0-5][0-9]' OR quiet_to GLOB '2[0-3]:[0-5][0-9]'), quiet_tz TEXT CHECK(quiet_tz IS NULL OR length(quiet_tz) BETWEEN 1 AND 64), quiet_always TEXT CHECK(quiet_always IS NULL OR quiet_always IN ('', 'approval', 'board', 'question', 'approval,board', 'approval,question', 'board,question', 'approval,board,question')), first_at REAL, first_ok INTEGER CHECK(first_ok IS NULL OR first_ok IN (0, 1)), first_error TEXT CHECK(first_error IS NULL OR length(first_error) <= 200), second_at REAL, second_ok INTEGER CHECK(second_ok IS NULL OR second_ok IN (0, 1)), second_error TEXT CHECK(second_error IS NULL OR length(second_error) <= 200), first_gen INTEGER NOT NULL DEFAULT 0, second_gen INTEGER NOT NULL DEFAULT 0, CHECK((second_url IS NULL) = (second_after_s IS NULL)), CHECK((quiet_from IS NULL) = (quiet_to IS NULL) AND (quiet_to IS NULL) = (quiet_tz IS NULL) AND (quiet_tz IS NULL) = (quiet_always IS NULL)), CHECK(quiet_from IS NULL OR quiet_from != quiet_to), CHECK((first_at IS NULL) = (first_ok IS NULL) AND (first_ok = 1) <= (first_error IS NULL)), CHECK((second_at IS NULL) = (second_ok IS NULL) AND (second_ok = 1) <= (second_error IS NULL)));
INSERT INTO "webhooks" VALUES(1,0,'https://hooks.example.com/services/T000/B000/ownersecret?token=t0',300.0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,0);
CREATE UNIQUE INDEX memberships_active_name ON memberships(room_id, screen_name) WHERE left_at IS NULL;
CREATE UNIQUE INDEX memberships_active_part ON memberships(room_id, participant_id) WHERE left_at IS NULL;
CREATE INDEX messages_room_id ON messages(room_id, id);
CREATE INDEX messages_meeting ON messages(meeting_id) WHERE meeting_id IS NOT NULL;
CREATE INDEX messages_agent_ts ON messages(room_id, ts) WHERE kind='chat' AND sender_kind='agent';
CREATE INDEX batches_wake_created ON batches(created_at) WHERE kind='wake';
CREATE INDEX deliveries_open ON deliveries(membership_id, state, message_id);
CREATE INDEX events_kind_ts ON events(kind, ts);
CREATE UNIQUE INDEX people_active_name ON people(name) WHERE removed_at IS NULL;
CREATE UNIQUE INDEX people_email ON people(email) WHERE removed_at IS NULL AND email IS NOT NULL;
CREATE INDEX reviews_room ON reviews(room_id, id);
CREATE INDEX claims_member ON claims(membership_id);
CREATE UNIQUE INDEX agent_tokens_device ON agent_tokens(device) WHERE revoked_at IS NULL;
CREATE INDEX listener_keys_token ON listener_keys(token_id, last_used_at);
CREATE UNIQUE INDEX meetings_one_open ON meetings(room_id) WHERE state!='ended';
CREATE UNIQUE INDEX meetings_key ON meetings(key_hash) WHERE key_hash IS NOT NULL;
CREATE INDEX files_room ON files(room_id);
CREATE INDEX files_message ON files(message_id);
CREATE INDEX file_grants_file ON file_grants(file_id);
CREATE INDEX room_board_changes_room ON room_board_changes(room_id, seq);
CREATE VIEW search_source AS SELECT id, text FROM messages WHERE kind='chat' AND sender_kind IN ('human','agent','meeting');
CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages BEGIN INSERT INTO messages_fts(rowid, text) SELECT new.id, new.text WHERE new.kind='chat' AND new.sender_kind IN ('human','agent','meeting'); END;
CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages BEGIN INSERT INTO messages_fts(messages_fts, rowid, text) SELECT 'delete', old.id, old.text WHERE old.kind='chat' AND old.sender_kind IN ('human','agent','meeting'); END;
CREATE TRIGGER messages_fts_au AFTER UPDATE ON messages BEGIN INSERT INTO messages_fts(messages_fts, rowid, text) SELECT 'delete', old.id, old.text WHERE old.kind='chat' AND old.sender_kind IN ('human','agent','meeting'); INSERT INTO messages_fts(rowid, text) SELECT new.id, new.text WHERE new.kind='chat' AND new.sender_kind IN ('human','agent','meeting'); END;
CREATE INDEX say_keys_expires ON say_keys(expires_at);
CREATE INDEX mcp_sessions_opened ON mcp_sessions(token_id, opened_at);
CREATE INDEX messages_addressed ON messages(room_id, id) WHERE kind='chat' AND (mentions!='[]' OR reply_to IS NOT NULL);
PRAGMA writable_schema=OFF;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('messages',2);
INSERT INTO "sqlite_sequence" VALUES('webhooks',1);
COMMIT;

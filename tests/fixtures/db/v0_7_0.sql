BEGIN TRANSACTION;
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
INSERT INTO "batches" VALUES(1,5,'read','pull',NULL,NULL,0,'confirmed',1790000007.0,1790000007.0,1790000007.5,NULL,NULL,NULL,NULL,'next_call');
INSERT INTO "batches" VALUES(2,1,'hook_ups','priority',NULL,NULL,0,'offered',1790000008.5,1790000008.5,NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO "batches" VALUES(3,4,'wait','pull',NULL,NULL,0,'offered',1790000013.0,1790000013.0,NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO "batches" VALUES(4,2,'read','pull',NULL,NULL,0,'expired',1790000015.0,1790000015.0,NULL,1790000015.0,'disconnect',NULL,NULL,NULL);
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
INSERT INTO "deliveries" VALUES(1,9,2,1,'offered',2,1,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(1,10,1,1,'offered',2,0,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(1,12,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(1,13,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(1,17,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(2,9,2,0,'pending',NULL,1,1,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(2,10,0,0,'pending',NULL,1,1,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(2,11,0,0,'pending',NULL,1,1,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(2,12,0,0,'pending',NULL,1,1,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(2,13,0,0,'pending',NULL,1,1,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(2,17,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(3,9,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(3,10,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(3,11,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(3,12,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(3,13,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(3,17,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(4,14,2,0,'offered',3,1,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(5,9,2,1,'handled',1,1,0,NULL,1790000007.5,1790000007.5,0,0);
INSERT INTO "deliveries" VALUES(5,11,1,1,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(5,12,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(5,13,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(5,17,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(6,9,2,0,'revoked',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(6,10,0,0,'revoked',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(6,11,0,0,'revoked',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(6,12,0,0,'revoked',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(6,13,0,0,'revoked',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(7,9,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(7,10,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(7,11,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(7,13,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(7,17,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(8,9,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(8,10,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(8,11,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(8,12,0,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
INSERT INTO "deliveries" VALUES(8,17,2,0,'pending',NULL,NULL,0,NULL,NULL,NULL,0,0);
CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, room_id INTEGER,
  membership_id INTEGER, participant_id INTEGER, kind TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}');
INSERT INTO "events" VALUES(1,1790000002.0,1,1,1,'join','{"harness": "claude", "tier": "claude:hook"}');
INSERT INTO "events" VALUES(2,1790000002.5,1,2,2,'join','{"harness": "codex", "tier": "mcp-only"}');
INSERT INTO "events" VALUES(3,1790000003.0,1,3,3,'join','{"harness": "cursor", "tier": "cursor:stop-park"}');
INSERT INTO "events" VALUES(4,1790000003.5,2,4,4,'join','{"harness": "devin", "tier": "devin:wait-loop"}');
INSERT INTO "events" VALUES(5,1790000004.0,1,5,5,'join','{"harness": "test", "tier": "mcp-only"}');
INSERT INTO "events" VALUES(6,1790000004.5,1,6,6,'join','{"harness": "unknown", "tier": "mcp-only"}');
INSERT INTO "events" VALUES(7,1790000005.0,1,7,7,'join','{"harness": "claude", "tier": "claude:inbox"}');
INSERT INTO "events" VALUES(8,1790000005.5,1,8,8,'join','{"harness": "claude", "tier": "claude:inbox"}');
INSERT INTO "events" VALUES(9,1790000006.0,1,1,1,'parked','{"reason": "idle and not listening: call wait() or poke it"}');
INSERT INTO "events" VALUES(10,1790000006.0,1,2,2,'parked','{"reason": "codex binary not found (or not owned by you or root)"}');
INSERT INTO "events" VALUES(11,1790000006.0,1,3,3,'parked','{"reason": "stopped and not parked (no stop hook waiting)"}');
INSERT INTO "events" VALUES(12,1790000006.0,1,6,6,'parked','{"reason": "idle and not listening: call wait() or poke it"}');
INSERT INTO "events" VALUES(13,1790000006.0,1,7,7,'parked','{"reason": "idle and not listening: call wait() or poke it"}');
INSERT INTO "events" VALUES(14,1790000006.0,1,8,8,'parked','{"reason": "idle and not listening: call wait() or poke it"}');
INSERT INTO "events" VALUES(15,1790000007.0,1,5,5,'offer','{"batch_id": 1, "path": "read", "n": 1, "counted": false, "ids": [9]}');
INSERT INTO "events" VALUES(16,1790000007.5,1,5,5,'confirm','{"batch_id": 1, "evidence": "next_call"}');
INSERT INTO "events" VALUES(17,1790000008.5,NULL,NULL,1,'turn_start','{"src": "hook"}');
INSERT INTO "events" VALUES(18,1790000008.5,NULL,NULL,1,'status','{"frm": "idle", "to": "busy", "src": "hook:UserPromptSubmit"}');
INSERT INTO "events" VALUES(19,1790000008.5,1,1,1,'offer','{"batch_id": 2, "path": "hook_ups", "n": 2, "counted": false, "ids": [9, 10]}');
INSERT INTO "events" VALUES(20,1790000012.0,NULL,NULL,NULL,'login','{"what": "claim", "via": "web", "passkey": "MacBook Touch ID"}');
INSERT INTO "events" VALUES(21,1790000012.0,NULL,NULL,NULL,'machine','{"what": "approve", "name": "work-laptop", "via": "web"}');
INSERT INTO "events" VALUES(22,1790000013.0,2,4,4,'offer','{"batch_id": 3, "path": "wait", "n": 1, "counted": false, "ids": [14]}');
INSERT INTO "events" VALUES(23,1790000015.0,1,2,2,'offer','{"batch_id": 4, "path": "read", "n": 5, "counted": false, "ids": [9, 10, 11, 12, 13]}');
INSERT INTO "events" VALUES(24,1790000015.0,1,NULL,NULL,'hops','{"limit": 6}');
INSERT INTO "events" VALUES(25,1790000015.0,NULL,NULL,1,'model','{"model": "claude-test-model"}');
INSERT INTO "events" VALUES(26,1790000015.0,1,3,3,'pass','{"handled": 0, "note_len": 0}');
INSERT INTO "events" VALUES(27,1790000015.0,NULL,NULL,NULL,'remote','{"what": "enable", "name": "fpga-pi", "via": "cli"}');
INSERT INTO "events" VALUES(28,1790000016.0,1,6,6,'unparked','{"seconds": 10.0}');
INSERT INTO "events" VALUES(29,1790000016.0,1,2,2,'unparked','{"seconds": 10.0}');
CREATE TABLE link_machines(
  name TEXT PRIMARY KEY, key BLOB NOT NULL, key_fp TEXT NOT NULL,
  facts TEXT NOT NULL DEFAULT '{}',
  rooms TEXT NOT NULL DEFAULT '["*"]',
  harnesses TEXT NOT NULL DEFAULT '["claude","codex","cursor","devin"]',
  created_at REAL NOT NULL, approved_at REAL, approved_via TEXT CHECK(approved_via IN ('cli','web')),
  removed_at REAL, last_seen_at REAL);
INSERT INTO "link_machines" VALUES('work-laptop',X'6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B6B','SHA256:workLaptopFingerprintMadeUp000000000000000','{"harnesses": ["claude", "codex"], "os": "darwin", "version": "0.7.0"}','["*"]','["claude","codex","cursor","devin"]',1790000002.0,1790000002.0,'web',NULL,1790000002.0);
INSERT INTO "link_machines" VALUES('lab-box',X'6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C6C','SHA256:labBoxFingerprintMadeUp0000000000000000000','{"os": "linux"}','["*"]','["claude","codex","cursor","devin"]',1790000002.0,NULL,NULL,NULL,NULL);
INSERT INTO "link_machines" VALUES('old-desk',X'','','{}','["*"]','["claude","codex","cursor","devin"]',1790000002.0,1790000002.0,'cli',1790000002.0,NULL);
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
INSERT INTO "memberships" VALUES(1,1,1,'vivado','aeaea68ebde91891117acd280e297c6fe957819065c038a5f102bd5dad8588fb',1790000002.0,0,NULL,NULL,0,0,NULL,0,-1);
INSERT INTO "memberships" VALUES(2,1,2,'codex-1','c345cbb1b03f95fa9b0479bbe105c19a960347919b3f2262ee7acc12d58dfd65',1790000002.5,1,NULL,NULL,0,0,NULL,0,-1);
INSERT INTO "memberships" VALUES(3,1,3,'cursor-1','ba178e296cec9b49f741a6be7ba555b1e2783d09d8a5954c35ae7121d5a65532',1790000003.0,2,NULL,NULL,0,1,1790000016.0,0,-1);
INSERT INTO "memberships" VALUES(4,2,4,'devin-1','2c3190784c0e5d14061dc0615079083d4b9adb967fdb61674446a63d9b0408bc',1790000003.5,0,NULL,NULL,0,0,NULL,0,-1);
INSERT INTO "memberships" VALUES(5,1,5,'bot-a','3a951aa46273b7e441fffddcf745e6aa319d927dd6cb0a20eab8188d8227b13a',1790000004.0,3,NULL,NULL,0,0,NULL,9,-1);
INSERT INTO "memberships" VALUES(6,1,6,'helper',NULL,1790000004.5,5,1790000016.0,'session_end',0,0,NULL,0,-1);
INSERT INTO "memberships" VALUES(7,1,7,'bench','4103e608bafe19b80d9446bf55d89174ce168bf10eb1e7f41940cd36dcaa2ab8',1790000005.0,6,NULL,NULL,0,0,NULL,0,-1);
INSERT INTO "memberships" VALUES(8,1,8,'laptop-1','0eadb9535e1b04cc2b0e628132b00ccf5095e9a8ef562795aab9226214fb8510',1790000005.5,7,NULL,NULL,0,0,NULL,0,-1);
CREATE TABLE messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL REFERENCES rooms(id), ts REAL NOT NULL,
  sender_membership_id INTEGER,
  sender_name TEXT NOT NULL, sender_harness TEXT,
  sender_kind TEXT NOT NULL CHECK(sender_kind IN ('human','agent','system')),
  via TEXT NOT NULL CHECK(via IN ('web','cli','mcp','system')),
  kind TEXT NOT NULL DEFAULT 'chat' CHECK(kind IN ('chat','join','leave','notice')),
  text TEXT NOT NULL, reply_to INTEGER, mentions TEXT NOT NULL DEFAULT '[]',
  sender_host TEXT);
INSERT INTO "messages" VALUES(1,1,1790000002.0,1,'vivado','claude','agent','mcp','join','joined (claude, claude:hook)',NULL,'[]',NULL);
INSERT INTO "messages" VALUES(2,1,1790000002.5,2,'codex-1','codex','agent','mcp','join','joined (codex, mcp-only)',NULL,'[]',NULL);
INSERT INTO "messages" VALUES(3,1,1790000003.0,3,'cursor-1','cursor','agent','mcp','join','joined (cursor, cursor:stop-park)',NULL,'[]',NULL);
INSERT INTO "messages" VALUES(4,2,1790000003.5,4,'devin-1','devin','agent','mcp','join','joined (devin, devin:wait-loop)',NULL,'[]',NULL);
INSERT INTO "messages" VALUES(5,1,1790000004.0,5,'bot-a','test','agent','mcp','join','joined (test, mcp-only)',NULL,'[]',NULL);
INSERT INTO "messages" VALUES(6,1,1790000004.5,6,'helper','unknown','agent','mcp','join','joined (unknown, mcp-only)',NULL,'[]',NULL);
INSERT INTO "messages" VALUES(7,1,1790000005.0,7,'bench','claude','agent','mcp','join','joined (claude on fpga-pi, claude:inbox)',NULL,'[]','fpga-pi');
INSERT INTO "messages" VALUES(8,1,1790000005.5,8,'laptop-1','claude','agent','mcp','join','joined (claude on work-laptop, claude:inbox)',NULL,'[]','work-laptop');
INSERT INTO "messages" VALUES(9,1,1790000006.0,NULL,'alice',NULL,'human','web','chat','@vivado build blinky and hand it to @bot-a',NULL,'["bot-a", "vivado"]',NULL);
INSERT INTO "messages" VALUES(10,1,1790000007.5,5,'bot-a','test','agent','mcp','chat','artifact: blinky/top.bit size:1024',9,'["vivado"]',NULL);
INSERT INTO "messages" VALUES(11,1,1790000010.0,1,'vivado','claude','agent','mcp','chat','built; @bot-a please flash it',NULL,'["bot-a"]',NULL);
INSERT INTO "messages" VALUES(12,1,1790000011.0,7,'bench','claude','agent','mcp','chat','flashed; UART shows PORT_OK 8080',11,'[]','fpga-pi');
INSERT INTO "messages" VALUES(13,1,1790000012.0,8,'laptop-1','claude','agent','mcp','chat','the UART log looks right to me',12,'[]','work-laptop');
INSERT INTO "messages" VALUES(14,2,1790000013.0,NULL,'alice',NULL,'human','cli','chat','devin-1: review the UART test when you can',NULL,'[]',NULL);
INSERT INTO "messages" VALUES(15,1,1790000015.0,NULL,'switchboard',NULL,'system','system','notice','#build: hop limit set to 6',NULL,'[]',NULL);
INSERT INTO "messages" VALUES(16,1,1790000016.0,6,'helper','unknown','agent','system','leave','left (session ended)',NULL,'[]',NULL);
INSERT INTO "messages" VALUES(17,1,1790000016.0,NULL,'alice',NULL,'human','web','chat','thanks all',NULL,'[]',NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "meta" VALUES('schema_version','3');
INSERT INTO "meta" VALUES('owner_handle','00112233445566778899aabbccddeeff');
INSERT INTO "meta" VALUES('owner_claimed_at','1790000001.0');
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
INSERT INTO "participants" VALUES(1,'claude','claude:41001@1789999400.00','00000000-0000-4000-8000-00000000c1a0',41001,1789999400.0,41008,1789999400.5,'/tmp/yk-inbox-41001.sock','bound',NULL,0,'busy',1790000008.5,'hook:UserPromptSubmit','claude:hook',NULL,'prompting',0,NULL,0,NULL,0,0,NULL,0,0,1790000009.0,NULL,1790000001.0,1790000009.0,NULL,'');
INSERT INTO "participants" VALUES(2,'codex','codex:00000000-0000-4000-8000-0000000c0de1','00000000-0000-4000-8000-0000000c0de1',42001,1789999400.0,42008,1789999400.5,NULL,'bound',NULL,1,'offline',1790000016.0,'mcp:bye','mcp-only',NULL,'bypass',0,NULL,0,NULL,0,0,NULL,0,0,NULL,NULL,1790000001.0,1790000001.0,NULL,'');
INSERT INTO "participants" VALUES(3,'cursor','cursor:conv-00000000-0001','conv-00000000-0001',43001,1789999400.0,43008,1789999400.5,NULL,'bound',NULL,0,'idle',1790000001.0,'join','cursor:stop-park',NULL,'unknown',0,NULL,0,NULL,0,0,NULL,0,0,NULL,NULL,1790000001.0,1790000001.0,NULL,'');
INSERT INTO "participants" VALUES(4,'devin','devin:44001@1789999400.00',NULL,44001,1789999400.0,44008,1789999400.5,NULL,'bound',NULL,0,'idle',1790000001.0,'join','devin:wait-loop',NULL,'unknown',0,NULL,0,NULL,0,0,NULL,0,0,NULL,NULL,1790000001.0,1790000001.0,NULL,'');
INSERT INTO "participants" VALUES(5,'test','test:bot-a',NULL,45001,1789999400.0,45008,1789999400.5,NULL,'bound',NULL,0,'busy',1790000001.0,'join','mcp-only',NULL,'unknown',0,NULL,0,NULL,0,0,NULL,0,0,NULL,NULL,1790000001.0,1790000001.0,NULL,'');
INSERT INTO "participants" VALUES(6,'unknown','unknown:46001@1789999400.00',NULL,46001,1789999400.0,46008,1789999400.5,NULL,'bound',NULL,0,'offline',1790000016.0,'session_end','mcp-only',NULL,'unknown',0,NULL,0,NULL,0,0,NULL,0,0,NULL,NULL,1790000001.0,1790000001.0,1790000016.0,'');
INSERT INTO "participants" VALUES(7,'claude','claude@fpga-pi:1000041001@1789999400.00','00000000-0000-4000-8000-00000000be9c',1000041001,1789999400.0,1000041008,1789999400.5,'/tmp/yk-inbox-remote.sock','bound',NULL,0,'idle',1790000001.0,'join','claude:inbox',NULL,'prompting',0,NULL,0,NULL,0,0,NULL,0,0,NULL,NULL,1790000001.0,1790000001.0,NULL,'fpga-pi');
INSERT INTO "participants" VALUES(8,'claude','claude@work-laptop:2000041001@1789999400.00','00000000-0000-4000-8000-0000001a9709',2000041001,1789999400.0,2000041008,1789999400.5,'/tmp/yk-inbox-laptop.sock','bound',NULL,0,'idle',1790000002.0,'join','claude:inbox',NULL,'prompting',0,NULL,0,NULL,0,0,NULL,0,0,NULL,NULL,1790000002.0,1790000002.0,NULL,'work-laptop');
CREATE TABLE passkeys(
  credential_id BLOB PRIMARY KEY, public_key BLOB NOT NULL,
  sign_count INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL, aaguid TEXT,
  created_at REAL NOT NULL, last_used_at REAL);
INSERT INTO "passkeys" VALUES(X'637265642D6C6170746F702D30303031',X'636F73652D6B65792D6C6170746F70',7,'MacBook Touch ID','adce0002-35bc-c60a-648b-0b25f1f05503',1790000001.0,1790000002.0);
INSERT INTO "passkeys" VALUES(X'637265642D70686F6E652D30303032',X'636F73652D6B65792D70686F6E65',0,'Phone',NULL,1790000002.0,NULL);
CREATE TABLE remotes(
  name TEXT PRIMARY KEY, config_hash TEXT NOT NULL,
  enabled_at REAL, enabled_via TEXT CHECK(enabled_via IN ('cli','web')),
  blocked_at REAL, blocked_reason TEXT, last_up_at REAL);
INSERT INTO "remotes" VALUES('fpga-pi','721fdebde8b43e1571f4b8f71469fe4ee088ccf4f7c13137571d5dc477717812',1790000001.0,'cli',NULL,NULL,1790000001.0);
CREATE TABLE rooms(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL, created_by TEXT NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0, paused_reason TEXT,
  budget_per_hour INTEGER NOT NULL, budget_remaining INTEGER NOT NULL, budget_window_start REAL NOT NULL,
  budget_notice_window REAL,
  hop_count INTEGER NOT NULL DEFAULT 0, hop_limit INTEGER NOT NULL,
  last_msg_at REAL);
INSERT INTO "rooms" VALUES(1,'#build',1790000000.0,'alice',0,NULL,60,60,1790000000.0,NULL,0,6,1790000016.0);
INSERT INTO "rooms" VALUES(2,'#review',1790000000.0,'alice',0,NULL,60,60,1790000000.0,NULL,0,6,1790000013.0);
CREATE TABLE web_sessions(id_hash TEXT PRIMARY KEY, created_at REAL NOT NULL,
  last_seen REAL NOT NULL, expires_at REAL NOT NULL,
  via TEXT);
INSERT INTO "web_sessions" VALUES('15ce7575cc6ba004d1495c9d880850c29e0fbef4b1754eb74b2735cb65bde10c',1790000006.0,1790000006.0,1790604806.0,'claim');
INSERT INTO "web_sessions" VALUES('9038b09220ac3d27e27a95eb3c58a218ec5d3a90d33222644b6d645ee01fcf76',1790000006.0,1790000006.0,1790604806.0,'passkey:Phone');
INSERT INTO "web_sessions" VALUES('851816345ddd0a5cd8aa29e934b78e668e9015aa8f2e810bc706fc8e68b51c78',1790000006.0,1790000006.0,1790604806.0,'login-link');
CREATE INDEX participants_host_mcp ON participants(host, mcp_pid) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX memberships_active_name ON memberships(room_id, screen_name) WHERE left_at IS NULL;
CREATE UNIQUE INDEX memberships_active_part ON memberships(room_id, participant_id) WHERE left_at IS NULL;
CREATE INDEX messages_room_id ON messages(room_id, id);
CREATE INDEX deliveries_open ON deliveries(membership_id, state, message_id);
CREATE INDEX events_kind_ts ON events(kind, ts);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('events',29);
INSERT INTO "sqlite_sequence" VALUES('messages',17);
INSERT INTO "sqlite_sequence" VALUES('batches',4);
COMMIT;

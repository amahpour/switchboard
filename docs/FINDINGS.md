# switchboard Milestone 0 findings

Recorded before the rename (the project was then called yakroom); commands and quoted strings use the current names.

Date: 2026-09-24. Machine: macOS 26.6 (Darwin 25.6.0), Apple M3, 8 cores.

Every M0 area was run once, then re-checked by an independent "verify" run that wrote its own scripts and changed the conditions. Where the two runs disagree, **this document follows the verify run**, and says so.

How to read the tables:
- **Verdict:** `pass`, `fail`, `partial` (works with conditions, or only part verified) or `inconclusive` (not measured).
- **Basis:** `E` experiment, `D` docs, `S` Apache-2.0 source read at the tagged release. `E+D` means both.
- **Re-verify:** what the independent run concluded: `confirmed`, `partial` (some claims corrected), `refuted`, or `not re-checked`.
- Latencies are `p50 / p95` with the sample count `n`. T0 is when the poster sent the message. "Turn start" is the first hook of the woken turn. "First action" is the model's first tool call, so it includes model time and depends on the model used.

---

## Summary

| Harness | Idle wake (chosen tier) | Turn start p50 / p95 | Mid-task priority path | Status |
|---|---|---|---|---|
| Claude Code | Inbox socket, posted by switchboard's MCP server (a live child of `claude`) with the token auth line and a ~0.3 s connection hold | 29–44 ms / 40–54 ms | Same socket (read at the next tool boundary); alternate: PostToolUse + PostToolUseFailure `additionalContext` | **Works.** In `bypassPermissions` this path skips the hold, so it needs an explicit per-session opt-in. The model acted on mid-turn messages only 6 of 13 times. |
| Codex CLI | App-server daemon, `turn/start` on unsubscribed connections | TUI shows it at 121–128 ms / 153–169 ms | `turn/steer` with `expectedTurnId` (next tool boundary, ~75 ms after the tool ends) | **Works when the TUI is attached to the daemon.** Attaching is conditional. Fallback is `codex queue` (≤10 s poll). |
| Cursor `agent` | Undecided: a Stop long-poll returning `followup_message` (unproven past a 38 s park), or an agent-started background waiter | 117 ms / 169 ms (Stop, n=6); 0.40 s / 0.52 s (waiter, n=6); **first run only** | `postToolUse` `additional_context` (≤10,000 characters) | **Partial.** Cursor latencies were not reproduced: the test account hit its usage limit during the verify run. |
| Devin CLI | No external idle wake. An MCP `wait()` loop keeps the agent in a turn; the experimental alternate is a background subagent | first action 1.05–1.20 s p50, 1.39–3.94 s p95 | PostToolUse `hookSpecificOutput.additionalContext` | **Partial.** The background-exec waiter fails. Every Devin tier makes the REPL look busy and queues typed input. |

Results that change the original spec's assumptions are in [Corrections to the original spec](#13-corrections-to-the-original-specs-assumptions). Open decisions are in [Decisions needed](#14-decisions-needed-before-m1).

---

## 1. Environment and versions

| Item | Version or setting |
|---|---|
| Claude Code | 2.1.281 (pinned binary, `DISABLE_AUTOUPDATER=1`). The global `claude` auto-updated to **2.1.282** during the first Claude inbox run. The verify runs re-tested idle wake, MCP-child delivery, holds and the Stop cap on 2.1.282 and saw the same behaviour. Stack-baseline ran on 2.1.282. Model: haiku (Haiku 4.5); one Claude hooks run used sonnet. |
| codex-cli | 0.156.1, source read at `rust-v0.156.1` (commit b412ff32). Models: gpt-6-luna (effort low or high) and gpt-5.5. |
| Cursor `agent` | 2026.09.23-86fc751, `--model auto` (auto routed to grok-4.5). |
| Devin CLI | 3000.11.3 (9c803229faa4), model swe-1-6-slow (the only model available to the test account). |
| Tools | python3 3.14.5 (Homebrew, SQLite 3.53.1), uv 0.7.13 and 0.10.8, uv CPython 3.13.5 (SQLite 3.47.1), fastmcp 4.0.9, mcp 2.2.0, fastapi 0.141.1, starlette 1.7.0, uvicorn 0.53.0, websockets 17.1, tmux 3.6a (private `-L` sockets only). |
| Launch hygiene | Every test CLI ran with `env -i` plus a minimal env. Config was project-local in scratch dirs or passed per invocation. tmux was used only as a test harness. |

---

## 2. Claude Code inbox socket (M0 point 1)

Docs: [cross-session-messaging](https://code.claude.com/docs/en/cross-session-messaging) (sections *the-sessions-inbox-socket*, *message-delivery*, *control-inbound-messages*, *how-a-session-treats-an-incoming-message*, *limitations*); [settings: crossSessionInbound, dialogExpiry](https://code.claude.com/docs/en/settings-reference#crosssessioninbound); [env-vars](https://code.claude.com/docs/en/env-vars); issue [#93720](https://github.com/anthropics/claude-code/issues/93720). Frame reference: daymade/claude-code-skills `peer.py` (MIT).

| # | Question | Verdict | Basis | Re-verify |
|---|---|---|---|---|
| 1.1 | Does a script's post make an idle, hand-started session start a turn within 2 s? | **pass** | E+D | confirmed on 2.1.281 and 2.1.282 |
| 1.2 | Frame format | **pass** | E+D | partial (core frame confirmed; silent drops and no-reply not re-checked) |
| 1.3 | Does the message land in the model's context? | **pass** | E+D | confirmed |
| 1.4 | Bypass mode (held?) | **partial**: held by default; own-child paths skip the hold | E+D | partial (recipe corrected, see below) |
| 1.5 | Permission prompt open: does a post cancel it (#93720)? | **pass**: not reproduced; the message waits | E | confirmed (reject and approve) |
| 1.6 | Busy session: is the message read between tool calls? | **partial**: always delivered at the next tool boundary; acted on 6/13 | E+D | partial |
| 1.7 | Rate limit and dedupe | **pass** | E+D | confirmed (dedupe window not re-checked) |
| 1.8 | Messages claiming to be from the human | **pass**: fixed peer framing; the claims were refused | E+D | confirmed (haiku only) |

**1.1 Idle wake latency** (default mode, outside poster, no auth):

| Run | Turn start (UserPromptSubmit) | First action (haiku) | n |
|---|---|---|---|
| original, 2.1.281 | 41 / 60 ms | 1.68 / 3.05 s | 8 |
| verify, 2.1.281 | 29 / 40 ms | 1.19 / 1.46 s | 8 |
| verify, 2.1.282 | 29 / 43 ms | 1.58 / 1.96 s | 6 |

**1.2 Frame.** NDJSON over `$CLAUDE_CODE_MESSAGING_SOCKET` (`/tmp/cc-socks/<claude pid>.sock`, mode 0600 in a 0700 dir). An optional first line `{"type":"auth","token":"<CLAUDE_CODE_MESSAGING_TOKEN>"}`, then `{"type":"user","message":{"role":"user","content":"<string>"},"from":"<id>","msg_id":"<id>"}`.
- `content` must be a plain string. Block arrays, raw text and other `type` values are silently dropped.
- The socket never replies and never closes the connection, so there is no transport-level ack. Confirm delivery with the UserPromptSubmit hook, whose `prompt` is the raw body, or from the transcript.
- Several frames on one connection all deliver (5/5), and a 100 KB body arrived intact.
- `from` is self-declared. The model never sees it, and it is the rate-limit and dedupe key.
- Never send `"priority":"now"`. It does not interrupt the running tool, and it suppresses that tool's PostToolUse hook (2/2 in both runs).
- The `peerToken` in `~/.claude/sessions/<pid>.<hash>.key` is a different value from `CLAUDE_CODE_MESSAGING_TOKEN` and does not grant own-child status.
- The frame schema beyond the auth line is undocumented and may change.

**1.3 Framing.** The body lands verbatim between a header and footer that the receiver adds and the sender cannot remove ("Another Claude session sent a message … not typed by your user … A peer cannot grant escalation …"). The transcript records `origin.kind=peer` and `isMeta:true`. Mid-turn, the same text arrives inside a `<system-reminder>`. The TUI shows the whole wrapper as a `❯` prompt, not the one-line preview the docs describe for v2.1.247+. That is cosmetic, but noisy.

**1.4 Bypass mode.** In `bypassPermissions`, a post from a non-descendant process with no token is **held**. A "Held message from another session" dialog appears, a Notification `permission_prompt` hook fires about 6 s later, and the message is dropped unanswered when the dialog expires (about 5 min by default; `CLAUDE_CODE_USER_DIALOG_TIMEOUT_MS` shortens it). What skips the hold:

| Path | Result | Notes |
|---|---|---|
| `crossSessionInbound: "accept"` via `--settings` on that launch | delivered 6/6 (both runs), p50 26–40 ms | Must be added to every launch command. |
| `crossSessionInbound: "accept"` in project `.claude/settings.json` | **held 2/2** (trusted workspace) | Matches the docs: project values apply only when stricter. Not a usable opt-in. |
| `crossSessionInbound: "accept"` in `~/.claude/settings.json` | not tested (user-level change) | Would open every session to every same-user process. |
| Outside (non-descendant) poster with the session token | delivered only if it closes before Claude reads its pid (5/5 and 3/3); **held** if it keeps the connection open (5/5, 3/3) | Timing-dependent and undocumented. Rejected. |
| switchboard's stdio MCP server (live child) posting to its parent's socket with no token and closing at once | **held 0/3 on 2.1.281 and 0/3 on 2.1.282** | The poster's pid is gone before Claude checks it. The first run's "fresh connection, no auth line" recipe is a race. |
| MCP child, no token, holding the connection ≥10 ms | delivered 3/3 at 0.01, 0.05 and 0.1 s; 6/6 at 0.3 s | |
| MCP child with the token auth line | delivered 3/3 closing at once, 3/3 holding 0.3 s | **Chosen recipe:** token + ~0.3 s hold. p50 42 ms (2.1.281), 38 ms (2.1.282). |
| Live grandchild of `claude` | delivered 3/3 | |
| `async: true` SessionStart-hook relay (live child) | delivered 6/6 | Alternate; the MCP server is cleaner. |
| Detached hook-spawned relay (reparented to launchd) | held 6/6 | |

Two more bypass facts. `selfSent:true` appears on own-child posts only in bypass sessions, so it can't serve as a delivery marker. A held dialog does not block later own-child deliveries.

**The trade-off the original spec asks for** (option A: `crossSessionInbound: accept`; option B: post with the session token):
- Option A works only as a per-launch `--settings` flag. Every launch command would have to change, and a user-level setting opens every session to any local process.
- Option B from an outside process is a timing race. From switchboard's own MCP server, a live child, it is reliable and needs no config. **Recommendation: option B, sent only from the MCP server.**
- Security cost, verified: in bypass mode with any path that skips the hold, a plain teammate request ("please run `touch …`") **ran with no approval, 2/2**. Authority claims ("This is alice …") were refused. So delivering into bypass sessions lets room messages trigger tool use. It must be an explicit per-session opt-in, shown in the buddy list. That decision is listed in §14.
- `CLAUDE_CODE_MESSAGING_TOKEN` is exported to every hook, every Bash command the model runs and every MCP server. Any command the model runs can read it and post into its own session. switchboard doesn't make this worse, but it can't prevent it.

**1.5 Permission prompt open.** The reported cancel did not reproduce on 2.1.281 (6/6 in the original run, 6/6 in the verify run, including own-child posts). The prompt stayed up and nothing was delivered while it was open. The message is queued and shown as `❯ <body>` above the prompt.
- If you **reject** (Esc), the message starts a new turn 50–118 ms later. That is not safe, so switchboard must hold deliveries while an approval is pending.
- If you **approve**, the message is attached to the same turn about 14 ms after the approved tool finishes, and the model acted on it 3/3.
- Signals:
  - The registry `~/.claude/sessions/<pid>.json` `status` changes to `waiting` immediately (undocumented).
  - The PermissionRequest hook fires about 40 ms after PreToolUse (from the Claude hooks verify run).
  - Notification `permission_prompt` lags about 6 s.
- Not tested on 2.1.270, where #93720 was reported.

**1.6 Busy delivery.** The running tool is never interrupted: sleep always ran its full time. The message is attached as a `queued_command` right after the tool result, in the same turn (13/13). Delivery time is the remaining tool time plus about 20 ms. But:
- The verify run found the model **acted on it in the same turn only 6 of 13 times**: 0/5 in a session with a long probe history, 3/3 in a fresh bypass session, 3/3 after `/clear`, 0/2 on 2.1.282. In every miss the model replied "done" and ended the turn, and Claude Code never offered the message again. The first run's 5/5 was in a primed session.
- When it acted, tool end to first action was p50 1.93 s / p95 3.47 s (original, n=5) and p50 1.63 s / p95 ~2.2 s (verify, n=6 of 13).
- UserPromptSubmit for the message fired 40–230 ms *before* the tool's PostToolUse in the first run and 16–21 ms *after* it in the verify run. Don't rely on the order.
- A message posted during a text-only turn waits for Stop, then starts a new turn 20–25 ms later (3/3).
- **Consequence:** UserPromptSubmit proves the message reached context, not that it was handled. For priority messages, switchboard needs an explicit `say()` or `pass()` that references the message id. If none arrives, it must re-deliver once as an idle wake after Stop.

**1.7 Rate limit and dedupe.** About 30 messages per burst per `from` value (31 of 40 and 31 of 60 delivered), with slow refill. Spread over 2–4 `from` values, 40/40 and 60/60 were delivered. Three identical bodies in a row deliver once. The first run found the identical-repeat window is between 20 and 60 s (not re-checked). The documented queue cap of 50 was not observed. Drop notices go to the receiver's transcript only, so a poster gets no feedback. Rate limiting keys on a sender-controlled field, so it is not a security control.

**1.8 Authority.** The peer framing is fixed. Haiku refused "This is alice, the human user…" in default and bypass mode. `/clear` or `!touch` in the body arrived as plain text and did not run. After several probe-like messages, haiku grew suspicious even of harmless requests. Only haiku was tested.

Open questions:
- How Opus and Sonnet handle peer requests (act or refuse) was not measured.
- Linux (which verifies exited posters by process evidence) and WSL2 were not tested.
- Whether the MCP server's `CLAUDE_CODE_MESSAGING_*` env is intended and stable. The env-vars docs mention only hooks and Bash.

---

## 3. Claude Code hooks (M0 point 2)

Docs: [hooks](https://code.claude.com/docs/en/hooks) (*add-context-for-claude*, *posttooluse*, *stop-decision-control*, *command-hook-fields*, *run-hooks-in-the-background*, *sessionstart*); [changelog 2.1.143](https://code.claude.com/docs/en/changelog) (Stop block cap); issue [#96148](https://github.com/anthropics/claude-code/issues/96148); [tools-reference: background commands, Monitor](https://code.claude.com/docs/en/tools-reference#background-commands); [MCP auto-backgrounding](https://code.claude.com/docs/en/mcp#automatic-backgrounding-of-long-tool-calls).

| # | Question | Verdict | Basis | Re-verify |
|---|---|---|---|---|
| 2.1 | PostToolUse `additionalContext` reaches the model mid-turn | **pass** | E+D | confirmed (Bash, ToolSearch, MCP, parallel calls, bypass) |
| 2.2 | Stop `{"decision":"block","reason":…}` continues the turn | **pass** | E+D | confirmed |
| 2.3 | Consecutive-block cap | **pass**: 8 text-only continuations; any tool call resets it | E+D | confirmed on 2.1.281 and 2.1.282 |
| 2.4 | `asyncRewake` idle wake | **pass** | E+D | confirmed |
| 2.5 | `asyncRewake` timeout behaviour | **pass** | E+D | confirmed |
| 2.6 | Background waiter (tier a) for Claude | **pass** | E+D | confirmed |
| 2.7 | SessionStart `additionalContext` (startup, resume, clear) | **pass** | E+D | confirmed |

**2.1 PostToolUse.** The hook prints `{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"…"}}` and exits 0. The model sees `<system-reminder>PostToolUse:<Tool> hook additional context: …</system-reminder>` before its next step. Two parallel tool calls each delivered their own context.
- A tool that **fails** fires **PostToolUseFailure**, not PostToolUse, and its `additionalContext` is also delivered. A permission-denied tool fires neither. switchboard must register both events, or priority messages skip every failing command.
- MCP tools are deferred behind ToolSearch in 2.1.281, so the first injection may land on ToolSearch.
- Whether the model *acts* on an embedded request depends on framing and model (first run only):
  - Imperative text was treated as prompt injection.
  - Chat framing with room context was followed after Bash, but declined after an MCP tool by both haiku and sonnet.
  - Purely informational injections were passed on every time.

**2.2–2.3 Stop block.** The reason arrives as a user message `Stop hook feedback:\n<reason>`. A system reminder also shows the model the **full hook command line**, so keep secrets off it. `stop_hook_active` is false on the first Stop and true on every chained Stop.
- **Cap:** text-only continuations stop at 8, and the 9th block is overridden with a notice. `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` changes the limit.
- **Reset:** any intervening tool call resets the counter. Runs of 12, 14 and 44 consecutive blocks with tool calls were never capped, so the cap gives no loop protection when the agent calls `say()`.

A synchronous Stop long-poll **holds your typed input** until it returns: UserPromptSubmit fires at once, but the message is processed only when the hook ends (about 17 s later, n=1 in each run).
- Esc sends the hook SIGTERM within 85–90 ms and ends the turn with no continuation.
- After a wake, the next Stop came about 1.0 s later (n=1, not a sampled claim).

Use it only as a last resort.

**2.4 asyncRewake.** Config: a Stop hook entry `{"type":"command","command":"…","asyncRewake":true,"timeout":N}`. It runs in the background at every Stop. Exit 2 wakes the idle session, and the model receives the hook's stderr (or stdout if stderr is empty) as `Stop hook blocking error from command "Stop": <text>`.

| Run | Turn start | First action | Ack command | n |
|---|---|---|---|---|
| original (default mode) | 53 / 131 ms | 1.71 / 2.23 s | 1.94 / 2.86 s | 8 |
| verify (default + bypass) | 67 / 84 ms | 2.00 / 2.87 s | 2.25 / 3.60 s | 14 |

It works unchanged in `bypassPermissions` (it is not held, unlike the inbox socket). Hazards, all verified:
- **Runaway (#96148):** any command that exits 2 immediately (a missing script, or `sh -c 'exit 2'`, even with empty output) wakes the session again about once per second, without limit. A missing executable (exit 127) or exit 1 does not. The listener needs an `sh` wrapper that exits 0 on every error, plus a broker-side wake budget.
- **Accumulation:** a new listener starts at every Stop and old ones never die (3 alive after 2 turns). A leader-file "supersede" design left 1 alive, and superseded listeners did not wake the session (verified).
- **Orphans:** `kill -9` of `claude` reparents listeners to pid 1. One later claimed a message and exited 2 to nobody, so the message was lost. `/exit` and SIGHUP do send them SIGTERM. Listeners must watch their parent pid, and a claimed message counts only as "offered" until a hook in the woken turn confirms it.
- **Esc:** an interrupted turn fires no Stop, so nothing re-arms. The gap is real only when the interrupted turn was started by that same listener's wake. With supersede de-duplication, 0 listeners are then armed (verified).
- **Permission prompt:** a wake that fires while a prompt is open is held, then starts a turn 0.1 s after you reject the prompt. The model misread it as "a stop hook blocked my command".

**2.5 Timeout.** At the timeout the listener gets SIGTERM (at 20.004 s for `timeout:20` and at 30 s for `timeout:30`). Nothing wakes and nothing is shown. The session stays unarmed until the next Stop, so pending messages must stay in the broker. The default is 600 s (verified). `timeout: 86400` is accepted: a 45-minute idle wake worked in the first run, and a 644 s-old listener woke the session in the verify run. No maximum was found.

**2.6 Background waiters in Claude** (idle session; turn start measured after the file or message appears):

| Waiter | Turn start (original · verify) | Ack (original · verify) | Re-arm |
|---|---|---|---|
| MCP `wait()` auto-backgrounded (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`; default 120 s) | 66 / 81 ms (n=8) · 58 / 83 ms (n=8) | 2.39 / 3.95 s · 4.10 / 5.02 s | re-call each wake |
| Monitor tool, one arming | 255 / 272 ms (n=8) · 262 / 300 ms (n=8) | 2.46 / 3.92 s · 2.70 / 4.67 s | none per message; max deadline 1800 s |
| Bash `run_in_background` that exits | 64 / 89 ms (n=8) · 72 / 78 ms (n=6) | 4.76 / 6.55 s · 6.32 / 7.88 s | every wake (the model re-armed 8/8 and 6/6) |

The Monitor deadline also wakes the idle session ("Monitor expired … Re-arm it …"), which costs one model turn per deadline. The background-Bash notification carries only an output-file path, so the model spends a Read first.

Open questions: `source=compact`, and whether listeners survive `/compact`, were not tested.

---

## 4. Codex CLI (M0 point 3)

Docs: [app-server](https://learn.chatgpt.com/docs/app-server.md); [hooks](https://learn.chatgpt.com/docs/hooks); [config reference](https://learn.chatgpt.com/docs/config-file/config-reference); [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). Source at `rust-v0.156.1`: `codex-rs/tui/src/daemon_startup.rs`, `startup_orchestration.rs`, `ext/queue/src/service.rs`, `hooks/src/engine/discovery.rs`, `hooks/src/config_rules.rs`, `core/src/hook_runtime.rs`, `core/src/mcp_tool_call.rs`.

### 4a. App-server daemon

| # | Question | Verdict | Basis | Re-verify |
|---|---|---|---|---|
| 3.1 | Does a hand-started TUI attach to the daemon? | **partial**: only if the daemon is already running (or `--enable daemon_auto_start`) and no disqualifying flag is passed | E+D+S | partial (thread-unload timing refuted) |
| 3.2 | Is `turn/start` visible in the TUI (idle wake)? | **pass** | E+D | confirmed |
| 3.3 | Is `turn/steer` delivered mid-turn and visible? | **pass** | E+D | confirmed; whether the model acts depends on framing |
| 3.4 | Is waiting-on-approval reported? | **pass** | E | confirmed |
| 3.5 | `codex queue` behaviour | **pass** as a fallback | E+S | confirmed |
| 3.6 | Is a Python client feasible? | **pass** | E | confirmed |

**Protocol.** JSON-RPC 2.0 without the `jsonrpc` field.
- Transports: stdio is JSONL. `unix://` and the daemon control socket (`~/.codex/app-server-control/app-server-control.sock`, a symlink to a 0600 socket in a 0700 dir) are **WebSocket over the Unix socket**; raw JSONL is closed without a reply.
- Handshake: `initialize`, then `initialized`.
- API: `thread/queue/*` needs `experimentalApi: true`. Everything else switchboard needs is stable.
- Client: an asyncio client of about 130 lines works, as does a stdlib hand-rolled WebSocket. So the client stays in **Python**, with a JSON-schema contract test per Codex version (`codex app-server generate-json-schema`).

**3.1 Attaching.** With the daemon running, a plain `codex -m … -a … -s …` attaches (`/status` shows "Server: Local background server"). It runs its own **embedded** app-server instead, invisible to switchboard, if given:
- any `-c` except the allowlisted `features.*` and `tui.fullscreen_transcript` keys;
- `--profile`, `--strict-config`, `--oss`, `--no-daemon` or `--dangerously-bypass-hook-trust`.

`--enable daemon_auto_start` makes a plain TUI start the daemon itself (socket up in 0.57–0.61 s). The config key `features.daemon_auto_start = true` is untested because it is a user-level change.
- **Stopping the daemon kills attached TUIs** ("Reconnect failed" about 15 s later in the verify run). switchboard must never stop or restart it.
- **Thread lifetime (corrected):** a thread unloads about **60–65 s after its last subscriber disconnects**, not after a 30-minute grace. A client holding a `thread/resume` subscription keeps a TUI-less thread loaded indefinitely (≥180 s observed), so a subscribed switchboard could wake **headless** turns. Verified: a `turn/start` on the thread of a TUI that had quit ran a full headless turn. After unload, `turn/start` fails with "thread not found", which fails safe.
- **Environment leak (new):** tool commands, hooks and MCP servers of an attached TUI run as children of the **daemon**, with the daemon's env, not the terminal's. With `daemon_auto_start`, the first TUI's env and cwd become everyone's: a second TUI saw `YK_PROBE=first_tui`. Secrets, PATH and venvs leak across sessions, and per-terminal env vars cannot identify a Codex session.
- A `--remote unix://` TUI takes the daemon's cwd and skips the folder-trust prompt.

**3.2 Idle wake with `turn/start`** on a hand-started, daemon-attached TUI. All 6 turns completed in both runs; the verify run used a fresh, never-subscribed connection per message:

| Run | `turn/started` | userMessage item | Visible in TUI | First model output | n |
|---|---|---|---|---|---|
| original | 12 / 26 ms | 109 / 122 ms | 128 / 169 ms | 1.01 / 2.00 s | 6 |
| verify | 15 / 25 ms | 107 / 125 ms | 121 / 153 ms | 1.11 / 3.61 s | 6 |

- The text renders as a normal `›` prompt, **indistinguishable from your own typing**, so the sender must be named inside the envelope.
- `clientUserMessageId` is echoed as the item's `clientId` (6/6), which supports a two-phase ack.
- `turn/start` also works before the TUI's first prompt, when `thread/resume` still fails.
- `turnTrigger` is accepted but shows up nowhere, so it can't carry attribution.
- A half-typed draft survives the wake.

**Never send overrides.** Any of these fields on `turn/start` or `thread/resume` persists on your session, including across your next typed turn, and the TUI adopts it:
- `turn/start`: `cwd`, `runtimeWorkspaceRoots`, `approvalPolicy`, `approvalsReviewer`, `sandboxPolicy`, `permissions`, `model`, `serviceTier`, `serviceTierForTurn`, `effort`, `summary`, `personality`, `collaborationMode`, `environments`, `disabledPluginIds`, `outputSchema`, `toolOutput`, `turnTrigger`.
- `thread/resume` also: `baseInstructions`, `developerInstructions`, `config`, `modelProvider`.
- Verified: a **widening** `approvalPolicy=never` on a read-only thread persisted.

**3.3 `turn/steer`** (requires `expectedTurnId`). It acks in p50 2.5 ms (original) and 6.1 ms (verify). The text is held until the running tool ends, then enters history as a userMessage in the same turn.

| Run | Tool end → in history | n |
|---|---|---|
| original | 70 / 82 ms | 5 |
| verify (human-started turn, unsubscribed sender) | 75.5 / 80.7 ms | 8 |

- End-to-end time is the remaining tool time plus about 75 ms, so a 10-minute build delays delivery by 10 minutes.
- `turn/start` while busy acts as a steer, and two concurrent `turn/start` calls merge into one turn.
- A wrong turn id or an idle thread returns `-32600`, which fails safe; fall back to `turn/start`.
- **Compliance caveat:** with neutral "[switchboard priority]" framing (tested as "[yakroom priority]") the model acted 6/6. With "[switchboard priority from @peer]" framing (tested as "[yakroom priority from @peer]") that conflicted with your instruction in the same turn, it acted **1 of 6**, although the steer was verifiably in history.
- Pending steers don't show in the TUI until the boundary.

**3.4 Approval state.** `thread/status/changed {active, activeFlags:["waitingOnApproval"]}` is sent, and `thread/read` shows the same flag to an unsubscribed client.
- `item/commandExecution/requestApproval` is **broadcast to every subscribed client**. The TUI kept its prompt only because the observer didn't answer; nothing in the protocol stops a second client from approving. Unsubscribed connections receive no approval requests.
- A `turn/steer` sent during an approval returned success and did not cancel the prompt. It was **silently lost** when you declined, and delivered and acted on when you approved.
- So switchboard must hold while `waitingOnApproval` and confirm delivery by `clientId`.

**3.5 `codex queue` / `thread/queue/add`.** The message has role user; its only metadata is `clientId`, with no sender field.

| Case | Latency (original · verify) | n |
|---|---|---|
| Daemon, idle loaded thread (TUI visible) | 133 / 140 ms · 165 / 179 ms | 5 + 5 |
| Embedded TUI, no daemon (SQLite poll, bound 10 s) | 5.7 / 5.9 s · 7.2 / 8.7 s (range 0.25–8.7 s) | 5 + 5 |
| Thread loaded in another process | 7.2 / 7.4 s · 7.2 / 9.2 s | 5 + 5 |

- It never steers: a busy thread gets a new turn 6–7 ms after the current one completes.
- It stalls after an interrupted turn until the next normal completion.
- An item for an unloaded thread waits for a `thread/resume`, and that turn then runs headless.
- `codex queue` accepts `-m`, `-s`, `--approve-for-me` and `--dangerously-*`. switchboard must never pass them.

### 4b. Hooks and sandbox

| # | Question | Verdict | Basis | Re-verify |
|---|---|---|---|---|
| 3.7 | How does hook trust review work? | **pass** | E+D+S | confirmed (the TUI is not silent) |
| 3.8 | Which tools does PostToolUse cover? (report: Bash only) | **pass**: Bash, `apply_patch` and MCP tools. The "Bash only" report is wrong for 0.156.1 | E+D+S | confirmed |
| 3.9 | PostToolUse `additionalContext` mid-turn | **partial**: always delivered; whether it is acted on depends on the model | E+D+S | partial |
| 3.10 | Stop `decision:block` continuation | **pass** as a mechanism; **partial** as a wake path | E+D+S | partial (latency higher, blocks the human) |
| 3.11 | Can hooks and the MCP server reach the broker under `workspace-write`? | **pass** (they are unsandboxed); the model's shell **cannot** | E+D+S | confirmed |
| 3.12 | Can hooks start a turn on an idle session? | **fail** | E+S | confirmed in the TUI |

**3.7 Trust.**
- Hooks load per config layer, from the `hooks.json` next to each `config.toml` and from `[hooks]` tables. Project layers load only in trusted projects.
- Trust is stored only in the **user** file `~/.codex/config.toml`, as `[hooks.state."<abs path>:<event>:<group>:<handler>"] trusted_hash="sha256:…"`. A project cannot pre-trust its own hooks.
- The hash covers the handler config (event, matcher, command, timeout, async), **not the script contents**.
- Inserting a group shifts the indices and un-trusts the groups after it; appending does not (verified).
- `codex exec` skips untrusted hooks silently. The **TUI** shows a "Hooks need review — N hooks are new or changed" dialog (Review / Trust all / Continue without trusting) and a persistent status-bar warning.

**3.8 Coverage.** PostToolUse fires for:
- Bash, including non-zero exits and sandbox denials;
- `apply_patch` (matcher aliases `Edit|Write`);
- successful MCP calls (`mcp__<server>__<tool>`).

It does **not** fire for an MCP call that times out, is refused for approval (PermissionRequest fires instead), or returns `isError: true`. switchboard tools must report normal outcomes, such as a `wait()` timeout, as normal results.

**3.9 additionalContext.** `hookSpecificOutput.additionalContext` is recorded as a **developer-role** message in the running turn. `systemMessage` never reaches the model, and `decision:block` on PostToolUse replaces the tool result. Whether the model acts on your mid-task request depends on the model:
- gpt-5.5 acted **12/12** in exec (Bash 6/6, MCP 3/3, apply_patch 3/3) and 1/1 in the TUI.
- gpt-6-luna acted **0/9** with room rules, 0/5 unprimed and 1/3 with imperative framing. The first run's "5/5 repeated the token" came from prompts that asked for the token.
- Latency with gpt-5.5 (n=12): post to injection p50 2.33 s / p95 5.18 s, bounded by tool cadence (~3.5 s per step); post to the requested command p50 5.59 s / p95 7.47 s.
- Context over about 2.5k tokens is cut in the **middle** (head and tail kept), and the full text spills to `$TMPDIR/hook_outputs/<thread>/…`. Keep batches well under 2k tokens.
- Injected context is invisible to you in the TUI.
- Hook overhead: p50 32–33 ms, p95 38–52 ms (n=51–53).

**3.10 Stop block.** It continues the same turn, and the reason arrives as a user-role message. `stop_hook_active` is the loop-guard field, and there is **no cap** (12/12 and 15/15 blocks honoured). The default hook timeout is 600 s with no maximum; a timed-out Stop hook is killed and the turn ends silently.
- **Wake latency** (socket long-poll to first action): p50 1.70 s / p95 3.14 s in the original run (n=6), but **p50 2.76 s / p95 4.77 s** in the verify run (n=10, two models; luna skipped the action once in 6).
- **In the TUI**, a parked Stop hook shows "Running hook" and **queues your typed message** until it returns. Esc kills the hook with no continuation and leaves a stale socket file.
- **Parallel hooks:** Stop hooks run in parallel and the continuation waits for the slowest, so any other trusted Stop hook, with its own timeout, can delay a switchboard continuation.

**3.11 Sandbox.** Under `-s workspace-write` and `read-only`, hooks and stdio MCP servers connected to a Unix socket in `/tmp`, because they are spawned outside the sandbox (docs: "Hooks are not sandboxed").
- The model's shell got **EPERM** for a Unix socket outside the workspace, one **inside** the workspace, and loopback TCP.
- Only `sandbox_workspace_write.network_access=true` (a widening switchboard must never apply) or `danger-full-access` let the shell connect.
- So a model-run waiter works only in sessions already configured with `danger-full-access` (or network access), not on the defaults.

**3.12 Idle wake via hooks.** Not possible. An `async: true` PostToolUse result that lands after the turn ends is held until the next user prompt (verified in the TUI with a 25 s watch). Idle wake has to come from `turn/start` or `codex queue`.

Open questions:
- Whether a background shell command's completion starts a new Codex turn (the tier-a "exit" mechanism) was **not tested**; only socket reachability was.
- In a daemon, is one MCP server process shared across threads? If so, per-call `_meta` is the only safe identity.
- Sandbox reach on Linux (landlock/bwrap) and WSL2 was not tested.
- Whether IDE-extension app-servers attach to the daemon was not tested.

---

## 5. Cursor Agent CLI (M0 point 4)

Docs: [hooks](https://cursor.com/docs/hooks); [third-party hooks](https://cursor.com/docs/reference/third-party-hooks); [CLI changelog](https://cursor.com/docs/cli/changelog); [MCP](https://cursor.com/docs/mcp), [CLI MCP](https://cursor.com/docs/cli/mcp); [headless](https://cursor.com/docs/cli/headless); [forum: 60 s MCP timeout](https://forum.cursor.com/t/agent-acp-mcp-tools-call-times-out-at-60s-with-no-way-to-configure-it/163925).

> **Blocker.** The verify run hit the Cursor test account's usage limit after 8 `-p` runs. So every model-dependent Cursor result below (TUI injection, follow-up loops, all wake latencies, background waiters, the MCP timeout run) rests on the **first run only** and was not reproduced independently. The verify run re-checked everything that needs no model turn and audited the first run's raw logs, and its recomputed statistics match. A live re-test is needed before M5.

| # | Question | Verdict | Basis | Re-verify |
|---|---|---|---|---|
| 4.1 | `postToolUse` `additional_context` mid-turn | **pass** | E+D | partial (`-p` re-verified 6/6; TUI not re-checked) |
| 4.2 | Stop `followup_message` and `loop_limit` | **pass** | E+D | partial (logs audited; new status rule) |
| 4.3 | Maximum Stop long-poll; can you type during it? | **partial** | E | partial (delivery after a >60 s park unproven) |
| 4.4 | Background waiter, exit-based: does it start an idle turn? | **pass** | E | not re-checked (quota) |
| 4.5 | Background waiter, output notification (`notify_on_output`) | **pass**, but slow after the first message | E | not re-checked (quota) |
| 4.6 | 60 s MCP tool timeout | **pass** (confirmed at 60.0 s; progress does not extend it) | E+D | partial (logs consistent; not re-run) |
| 4.7 | Hooks in headless `agent -p` | **partial**: no stop hook | E | confirmed |

**4.1** `{"additional_context":"…"}` from `postToolUse` (and `postToolUseFailure`) reached the model in the same turn:
- in the TUI after Shell and MCP tools, after a failed Read, and from Claude-format project hooks;
- in `-p` (2/2 original, 6/6 verify).

Details:
- The text is appended to the tool result inside `<system_reminder>`. It is not shown in the TUI or the transcript.
- **Cap: 10,000 characters.** Verified: 10,000 were delivered; 10,001 were dropped whole and silently; 7,000 two-byte characters (13,898 bytes) were delivered.
- `updated_mcp_tool_output` can rewrite a switchboard MCP result.
- The hook's `tool_name` for MCP tools is `MCP:<tool>`, without the server name.

**4.2** A non-empty `followup_message` auto-submits as the next user message (`<user_query>`, the same framing as your typing), and beforeSubmitPrompt does not fire for it.
- `loop_limit` omitted means 5 follow-ups, after which the stop hook is not called; `null` means unbounded (24 observed). `loop_count` resets on your next prompt.
- **New (docs + verify, 2/2):** `followup_message` is ignored unless the stop status is `completed`. A quota or network error gives `status=error`, and a message consumed then is lost.
- `hooks.json` is not hot-reloaded in the CLI, so `switchboard install` affects only new sessions.

**4.3 Stop long-poll.**
- The default stop-hook timeout is **60 s**. The wrapper was killed at 59.9 s in both runs, the Python child was orphaned, and a message it had consumed was lost.
- An explicit `"timeout"` of 900 or 7200 keeps the hook alive past 60 s. But **no run has shown a follow-up delivered after a park longer than 38.3 s** with status `completed`. The 300 s park returned `{}`, and the verify run's 71 s delivery had status `error` and was discarded. The recommended 900–3600 s park is unproven.
- While a hook is parked the TUI looks **idle**. You can type and submit (beforeSubmitPrompt fires 0.10–0.4 s after Enter), and your turn runs at once. The old parked hook is **not** cancelled: a new one parks next to it, and a stale one later delivered its follow-up. Parked hooks survive `/exit` and a kill of the TUI, and a ppid check doesn't notice, so a hook must poll the agent pid it captured at start.
- Ctrl+C fires stop twice (`aborted`, then `error`).
- Latency (first run, parks ≤38 s): the hook saw the file at p50 10 / p95 20 ms (n=13); the follow-up was visible at **117 / 169 ms** (n=6); first tool at **1.73 / 2.26 s** (n=13). Not reproduced.

**4.4–4.5 Background waiters** (first run only):
- **Exit-based** (a `block_until_ms: 0` shell job that exits on a message): 13/13 idle wakes; visible at **0.40 / 0.52 s** (n=6); first tool at **2.17 / 2.67 s** (n=13). It must be re-armed after every wake; the model did so 13/13 when told to. The model gets a user-role `<system_notification>` without the output, so it reads the terminal file.
- **`notify_on_output`** (pattern `^SWITCHBOARD .*`): one process served 13 wakes, with the message text inline. The first match was visible in 0.42 s, but every later match took **5.43 / 5.49 s** (n=5, a 5 s minimum debounce), with first tool at 7.0 / 9.1 s (n=11). This misses the 2 s target. The tool schema says to set it only when the user explicitly asks for monitoring.
- Both **drop the wake** if the last turn was aborted with Ctrl+C, and the waiter has already consumed the message, so it is lost. Ack only when the agent reads through MCP.
- Not tested: whether Cursor's shell sandbox lets a waiter connect to the broker socket (the experiments used a flag file).

**4.6 MCP timeout.**
- Calls of 30 and 55 s succeeded. A 70 s call was cancelled by the client at 60.0 s (3 samples: 60.0016, 59.955 and 60.0019 s) with `MCP error -32001: Request timed out`.
- Cursor sends no `progressToken`, and unsolicited progress notifications did not extend the limit.
- Calls to one server are **serialized** per agent process; a model-free control showed the FastMCP server itself handles calls concurrently. A queued call's reported duration includes its queue time, so a `wait()` queued behind another call may time out early.
- Keep `wait()` ≤50 s with one switchboard call in flight.

**4.7 Headless.** In `agent -p`, sessionStart, preToolUse, postToolUse(+Failure) and sessionEnd fire. **Stop never fires** (0/3 original, 0 in 8 completed verify runs), and neither do beforeSubmitPrompt and afterAgentResponse. Background shell completions do produce a follow-up turn in `-p` (first run).

Open questions:
- IDE behaviour (out of scope).
- `notify_on_output` over 30+ minutes idle.
- What happens when two parked hooks for one conversation both return.
- Whether peer text containing `</system_reminder>` or `</user_query>` breaks the framing. Escape it.

---

## 6. Devin CLI (M0 point 5)

Docs: [hooks overview](https://docs.devin.ai/cli/extensibility/hooks/overview), [lifecycle hooks](https://docs.devin.ai/cli/extensibility/hooks/lifecycle-hooks), [MCP permissions](https://docs.devin.ai/cli/extensibility/mcp/configuration#mcp-permissions), [permissions](https://docs.devin.ai/cli/reference/permissions), [changelog](https://docs.devin.ai/cli/changelog/stable). All runs used `read_config_from` all false and `--respect-workspace-trust false`.

| # | Question | Verdict | Basis | Re-verify |
|---|---|---|---|---|
| 5.1 | PostToolUse `additionalContext` | **pass** | E+D | confirmed |
| 5.2 | Stop `decision: block` | **pass** | E+D | confirmed |
| 5.3 | Hook timeout | **pass**: 60 s default for every event; the `timeout` field is honoured to at least 3600 s | E+D | partial (interrupt behaviour corrected) |
| 5.4 | Does the background waiter work? | **fail** (background exec) | E | confirmed; a background **subagent** does wake the agent (new) |
| 5.5 | How are MCP calls named in hooks? | **pass**: `mcp__<server>__<tool>`, not `mcp_call_tool` | E+D | confirmed |
| 5.6 | MCP tool timeout | **pass**: none observed up to 900 s | E | confirmed (extended from 310 s) |
| 5.7 | Stop long-poll as a wake | **partial** | E | partial (heavy tail) |
| 5.8 | MCP `wait()` loop as a wake | **pass**, with an orphan hazard | E | partial |

**5.1** Only the nested form works: `{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"…"}}`. A top-level `additionalContext` is silently ignored (verified).
- It lands as a **system-role** step right after the tool result and reaches the next model call about 0.6–0.9 s later.
- The model paraphrased it as "the user is now asking".
- It is not shown in the TUI.
- Hook runtime: 7–32 ms.

**5.2** The block reason is inserted as a **user-role** message and is not shown in the TUI. `stop_hook_active` is true on every chained Stop.
- There is **no cap**: 30 consecutive blocks in `-p`, 12 in the TUI.
- A reason starting with `/bypass` stayed plain text.
- No Stop fires when a turn ends by Esc interrupt or a rejected approval.
- **New:** Stop also fires when a background subagent finishes, with the parent's `session_id` and `prompt_id` and no agent id. A block returned there continues the **subagent**, and its new final text replaces what the main agent receives (verified). A switchboard Stop hook must not block while it knows a subagent is running.

**5.3 Timeout and interrupts.** With no `timeout`, the hook gets SIGTERM at 59.8–59.98 s. That applies to UserPromptSubmit as well as Stop, so a slow UserPromptSubmit hook delays the model call.
- `timeout: 10` and `15` killed at 9.97 and 14.98 s. With 900 and 3600, sleeps of 600 s and 1200 s finished.
- While a Stop hook runs, Esc Esc, Ctrl+C and Enter on an empty line ("send now") are **deferred** until it returns. The ACP cancel is sent at once but applied later.

Corrections from the verify run:
- An interrupted block is **not discarded**. It is saved to the transcript as a user message but not acted on in that turn; the model later said "I have not answered message id=13 yet". Envelopes need stable ids so re-delivery can be de-duplicated.
- Typing, send-now, then Esc Esc or Ctrl+C **lost your message**: it was shown in the TUI, is absent from the session DB, and was never answered. Send-now alone delivered it about 50–64 ms after the hook returned.

**5.4 Background waiter.** An agent-started `exec` with timeout 0 fired and exited 13 ms after the flag appeared, but produced **no hook, no turn and no notice** (30 s watch, both runs).

**New:** `run_subagent` with `is_background=true` does wake an idle main agent, with a system-role `<subagent_completion_notification>`.
- A subagent that runs `switchboard.wait` once and returns gave 7/7 wakes, first action p50 **3.27 s**, max 21.9 s (n=7, verify only).
- Cost: 2 extra model calls per wake.
- It needs `mcp__switchboard__*` pre-approved, because tools that aren't pre-approved are auto-denied in background subagents.
- While it runs, the REPL shows busy and queues your typing.

**5.5 MCP naming.** The model only sees the generic `mcp_call_tool` / `mcp_list_tools`, but hooks see `tool_name = mcp__<server>__<tool>`, with the inner arguments as `tool_input`. PreToolUse (only) also carries `tool_provenance {type:"mcp", server, path}`. In Normal mode an MCP call prompts with 7 options, including "Yes, switch to bypass mode"; `permissions.allow: ["mcp__yk__*"]` removes the prompt. A PermissionRequest hook fires about 44 ms after PreToolUse.

**5.6 MCP timeout.** Calls of 30, 70, 130, 310, 420 and 900 s all returned, without progress notifications. Devin sends a `progressToken`.
- A main-agent interrupt (Esc Esc or send-now) cancels the call in the UI but sends **no** `notifications/cancelled`. The orphaned server-side `wait()` later **consumed the next room message**, and Devin logged "Error sending response" (verified: the message was lost).
- A subagent's interrupted MCP call does send `notifications/cancelled` and fires PostToolUse with `success:false`.

**5.7–5.8 Wake latency** (T0 = message posted; measured to the first tool call of the reaction):

| Tier | Original | Verify | Notes |
|---|---|---|---|
| MCP `wait()` loop in the main agent | p50 1.05 s / p95 1.39 s (n=6) | p50 1.20 s / p95 3.94 s (n=6) | REPL shows busy; send-now interrupts in ~85–91 ms |
| Stop long-poll (`timeout` 900) | p50 1.25 s / p95 1.57 s (n=6) | p50 1.56 s / p90 1.94 s / **p95 15.6 s** (n=11) | Interrupts deferred; typed input starved |
| Background subagent listener | — | p50 3.27 s, max 21.9 s (n=7) | Experimental |

Only the p50s meet the 2 s target; the swe-1-6-slow model has a heavy tail.

Open questions:
- How `/resume` and `/fork` respawn the ACP process and MCP server.
- Devin Desktop and other ACP hosts.
- Whether Accept-Edits mode protects `.devin/hooks.v1.json` and `.claude/settings.json`; only `.devin/config.json` was shown to force a prompt.

---

## 7. Identity binding (M0 point 6)

**Claude Code: pass, with care** (basis: E)
- The MCP server's env has `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN` (2.1.281 and 2.1.282; undocumented).
- After `/clear`, the MCP server is **not restarted** and keeps the old session id, while hooks and the registry get the new one. The socket and token stay the same.
- After `--resume` in a new process, the session id stays the same but the pid and socket change.
- Tools/call `_meta` carries only `claudecode/toolUseId` and `progressToken`.
- **Bind on the claude pid or socket path plus the current session id**, taken from hooks (SessionStart `source=startup/resume/clear`, UserPromptSubmit) or `~/.claude/sessions/<ppid>.json`. SessionEnd `reason=clear` does not mean leaving the room.

**Codex CLI: pass** (basis: E+S)
- Every MCP `tools/call` carries `_meta.threadId`, which equals `sessionId`, `x-codex-turn-metadata.thread_id` and the hook stdin `session_id`, in exec and in the TUI.
- Hooks get no `CODEX_THREAD_ID` env var; the model's shell does.
- Per-terminal env cannot identify a daemon-attached session (env leak, §4a).

**Cursor `agent`: partial** (basis: E)
- Hook stdin `conversation_id` (= `session_id` = the `--resume` id) and the agent shell's `CURSOR_CONVERSATION_ID` agree.
- The MCP server gets **no** chat id: `_meta` is null, and `${env:CURSOR_CONVERSATION_ID}` stays literal in `mcp.json`. Only `${env:TMUX_PANE}` interpolates.
- Proposed, untested: bind by correlation. The `postToolUse` hook for `MCP:join` sees `conversation_id` plus the join's `tool_input` and result.
- `sessionStart` does not fire on `--resume`.
- The MCP server is per process, so a chat switch inside a TUI could make the binding stale (untested).

**Devin CLI: pass** (basis: E)
- Hooks report `session_id` and their parent pid (the `devin acp` process), and the MCP server's `getppid()` is the same pid.
- `/new` and `/clear` respawn both with a new session id, so the mapping is 1:1 in the REPL.
- The MCP handshake carries no session info.
- Subagent hooks carry the parent's ids.

---

## 8. Cross-import of `~/.claude` hooks (M0 point 7)

**Cursor: partial** (basis: E, plus research). Project scope was tested; user scope was only seen incidentally.
- Claude-format hooks from project `.claude/settings.json` run with **Cursor-shaped stdin** (`hook_event_name` `postToolUse`/`stop`/`beforeSubmitPrompt`, plus `cursor_version`) and `CURSOR_VERSION` in env. There is no `CLAUDE_CODE_*` marker, and `CLAUDE_PROJECT_DIR` **is set** under Cursor.
- A command string identical in the `.claude` and `.cursor` hooks ran **once**. In a verified control run, distinct strings both ran.
- User-level `~/.claude` hooks also fired under the test sessions, so user scope is imported.
- Per research (not re-tested): Notification and PermissionRequest are not imported, and a Claude Stop hook runs only if a native stop hook exists.

**Devin: inconclusive, not tested** (basis: research only).
- Both runs set `read_config_from` all false, so import and double-firing were never exercised.
- Research lead (from logs, not an experiment): Devin fails to parse a `~/.claude/settings.json` that uses the `SubagentStart` event ("unknown variant `SubagentStart`") and then imports no hooks from it.
- Detection signals for native Devin hooks: `DEVIN_PROJECT_DIR`, `CHISEL_SESSION_DB`, parent argv `devin acp`. `CLAUDE_PROJECT_DIR` is also set, and `CLAUDECODE` is absent.

Design consequences:
- Detect the harness from runtime signals, never from `CLAUDE_PROJECT_DIR`:
  - Claude: `CLAUDECODE=1` and `CLAUDE_CODE_SESSION_ID`;
  - Cursor: `cursor_version` in stdin, or `CURSOR_VERSION`;
  - Devin: `DEVIN_PROJECT_DIR` or `CHISEL_SESSION_DB`.
- The original spec's single script with `--harness X` flags gives **different command strings per harness**, so Cursor won't de-duplicate them and both copies will fire. Either install byte-identical command strings and detect the harness at runtime, or have a copy that finds itself under the wrong harness exit 0 at once.
- Before M2, test Devin import with `read_config_from.claude` true, and decide whether `switchboard install devin` sets it to false.

---

## 9. MCP tool-call timeouts (M0 point 8)

**Claude Code** (basis: E+D). Suggested `wait()` cap: long waits are fine; send progress notifications or set a per-server `timeout`.
- **Default:** no practical wall-clock limit. The docs give `MCP_TOOL_TIMEOUT` a default of about 28 h, and 30/70/130/310 s calls all completed. A silent call is aborted after the 30-minute stdio idle window, checked on a ~30 s tick. Interactive sessions **auto-background** a call at 120 s, and its completion wakes the idle session.
- **Configurable:** `MCP_TOOL_TIMEOUT` and the per-server `"timeout"` both cap the call. The per-server `"timeout"` also **raises** the idle window (a 45 s silent call survived idle=10 s); `MCP_TOOL_TIMEOUT` does not. Progress notifications reset the idle timer.
- **On timeout:** error text "timed out after Ns", and the server receives `notifications/cancelled`. Esc cancels within 65–70 ms.

**Codex CLI** (basis: E+D+S). Suggested cap: ≤55 s bounded server-side, or set `tool_timeout_sec`.
- **Default:** **300 s** (a source constant; the docs say 60 s). Calls of 30/70/130 s completed; 310 and 320 s failed at 300 s.
- **Configurable:** `tool_timeout_sec` in the server table, in both directions. The model cannot pass its own.
- **On timeout:** no PostToolUse and **no `notifications/cancelled`**, so the handler keeps running.

**Cursor `agent`** (basis: E+D; first run, logs audited). Suggested cap: ≤50 s with one call in flight.
- **Default:** **60 s**, fixed. Calls of 30 and 55 s succeeded; 70 s was cancelled at 60.0 s.
- **Configurable:** not by progress (Cursor sends no `progressToken`).
- **On timeout:** `MCP error -32001`, and postToolUseFailure fires. Calls per server are serialized.

**Devin CLI** (basis: E). Suggested cap: 240–300 s per call, with a newer call superseding older ones.
- **Default:** **none observed** up to 900 s.
- **On interrupt:** a main-agent interrupt sends no cancel, and the orphaned call consumed the next message.

---

## 10. Stack baseline

Docs: [SQLite WAL](https://www.sqlite.org/wal.html), [pragma synchronous/fullfsync/data_version](https://www.sqlite.org/pragma.html), [BUSY_SNAPSHOT](https://www.sqlite.org/rescode.html#busy_snapshot), [Starlette TrustedHost](https://www.starlette.io/middleware/#trustedhostmiddleware), [Claude channels reference](https://code.claude.com/docs/en/channels-reference).

| # | Question | Verdict | Basis | Re-verify |
|---|---|---|---|---|
| S1 | Python version | **pass**: uv-managed CPython 3.13, `requires-python >=3.12`, CI on 3.14 | E | confirmed |
| S2 | Current FastMCP version; 3.13/3.14 round trip | **pass**: fastmcp 4.0.9 (mcp 2.2.0). Pin it exactly and commit `uv.lock` (4 releases in 20 h) | E | confirmed |
| S3 | FastMCP custom capabilities and notifications (Channels) | **pass** | E+D | confirmed |
| S4 | Hook cold-start budget | **pass**, with a harness-wrapper caveat | E | confirmed |
| S5 | SQLite WAL across processes | **pass**; one transaction claim corrected | E+D | partial |
| S6 | Wake transport | **pass** for hooks and MCP servers; sandboxed waiters can't use a socket | E+D | partial |
| S7 | FastAPI + WebSocket on 127.0.0.1 | **pass** for browsers; two local-auth gaps | E+D | partial |

**S1. Python.**
- fastmcp 4.0.9's classifiers stop at 3.13, but it runs on 3.14.5.
- uv 0.7.13, first on PATH, can only fetch 3.14.0b2.
- Homebrew 3.14 runs any `sitecustomize` and editable-install `.pth` finders installed into it (about 7–8 ms at start here), plus outside code in our processes. Even a uv venv built on it spends about 5 ms there.
- FastMCP spawn to initialize: p50 367–391 ms on 3.13 and 420–447 ms on 3.14 (n=12).
- The first `tools/call` after start costs 85–297 ms; later calls about 0.5 ms.

**S3. Channels.** A FastMCP server can declare `experimental: {"claude/channel": {}}` and push `notifications/claude/channel`.
- With `--dangerously-load-development-channels`, a channel event **woke an idle Claude Code 2.1.282 session 6/6 in both runs**. Rendered at p50 83 ms (original) and 103 ms (verify); haiku replied at p50 0.98 s and 1.11 s (n=6 each). So the original spec's "idle-wake bug" does not reproduce on 2.1.282.
- Channels stay demoted anyway. The non-dangerous `--channels server:<name>` drops pushes ("not on the approved channels allowlist"), and meta keys become attributes of the `<channel …>` prompt tag.
- FastMCP's own Python client silently drops custom notifications.

**S4. Hooks.** The test hook is stdlib-only: it reads stdin, opens a WAL db of 20k–200k rows, finds unread rows and prints JSON. Run as `"<abs venv python>" -I -S "<abs>/switchboard_hook.py"`, it costs p50 17.7–21.0 ms and p95 ≤25.3 ms (n=40 per variant, 2–3 runs, also under a writer doing 20 commits/s and with a 40 MB WAL).
- Asking the broker over a Unix socket instead costs about the same (p50 18.3–21.8 ms).
- Python's floor is 12–13.6 ms, so "inert in a few ms" needs a `/bin/sh` prefilter (3.6–4.4 ms).
- Importing fastmcp and fastapi costs 221–234 ms (769 ms on the first run). `uv run` costs 25–40 ms warm and 70–300 ms on the first run, and may touch the network.
- **Caveat:** if a harness spawns hooks through a login shell (`$SHELL -lc`), the login profile adds about 200 ms (bash) to 400 ms (zsh) before the hook starts (measured on the test machine). The M0 Codex hooks run found that Codex uses `$SHELL -lc` but measured only about 33 ms, so this needs reconciling in M2. Cursor runs hooks through a snapshot of the login shell (`bash -O extglob -c …eval "$snap"`).

**S5. SQLite.**
- WAL readers never block the writer, and the writer never blocks readers: 4–8 readers, some holding 1.5–2 s snapshots, with 0 errors.
- Commit latency: `synchronous=NORMAL` p50 0.015–0.13 ms; FULL about 0.09–0.17 ms, which gives no real power-loss durability on macOS; FULL with `fullfsync` p50 3.1–3.8 ms.
- `busy_timeout=5000` (Python's default `timeout=5.0`) kept 4–6 concurrent writers at 0 errors; `timeout=0` failed 81–98% of commits.
- **Correction:** with Python's default `isolation_level` (`''` or `'DEFERRED'`), a read-then-write does **not** raise `SQLITE_BUSY_SNAPSHOT`. It **silently loses the other connection's update**. BUSY_SNAPSHOT happens only with `autocommit=False` or an explicit deferred `BEGIN`. Use `isolation_level=None` plus `BEGIN IMMEDIATE` on every write path.
- A long-lived reader stops checkpoints: the WAL reached 18–27 MB after 3000 commits.
- A hook that opens the db read-write and is the last connection checkpoints it and deletes `-wal` and `-shm` on close. Hooks must open with `file:…?mode=ro`.

**S6. Wake transport.**
- Unix-socket long-poll: p50 0.35 / p95 0.84 ms for a bare asyncio broker (n=120). On the real FastAPI/uvicorn stack with 5 waiters and persist-then-wake: **p50 2.27 / p95 4.13 ms** (n=600).
- Polling every 50 ms: p50 26–31 / p95 50–57 ms, at about 1.3–2 ms of CPU per second. Every 250 ms: p50 108 / p95 200 ms, at about 0.3 ms of CPU per second.
- Socket path limits: macOS `sun_path` max is 103 bytes (tested); Linux is 107 (docs).
- **Correction:** under Codex's `:workspace` seatbelt, a model-run `switchboard wait` gets EPERM on any Unix-socket connect **even while the broker is up**. It can open the db read-only only while the broker holds it open. So a sandboxed waiter must poll `PRAGMA data_version` read-only (sandboxed 50 ms poll: p50 36 / p95 55 ms, n=20), and "fall back when the broker is down" doesn't apply there.

**S7. Web UI.** The design is a TrustedHost allowlist, a one-time terminal login token exchanged for an HttpOnly SameSite=Strict cookie, exact Origin plus a custom `X-Switchboard` header on writes, and a WebSocket Origin and cookie check before accept.
- It passed 14/14 checks in the original run and 18/18 in the verify run, on 3.13 and 3.14. lsof shows it listening only on `127.0.0.1`.
- POST to WebSocket receive: p50 3.2–3.4 ms (n=40 and n=200). The first run's 0.6–0.9 ms were single samples.
- Two gaps (§11): the cookie leaks to other `127.0.0.1` ports, and a 0600 Unix socket authorises every same-user process, including agents.

---

## 11. Security findings (original spec rule: a chat message must never make an agent skip approvals or widen its sandbox)

None of the delivery mechanisms, on its own, skipped an approval prompt or widened a sandbox. Slash commands, `!` and `/bypass` text, and authority claims in a message were never executed as commands. But several mechanisms **can** approve or widen if switchboard, or any same-user process, uses them, and several deliver messages with high authority. Items marked **must** are requirements for M2 onward.

**Mechanisms that approve or widen (switchboard must never use them):**
- **Codex hooks:** verdict **fail**, because Codex does not enforce the guardrail.
  - A trusted PermissionRequest hook returning `{"decision":{"behavior":"allow"}}` auto-approved an MCP call that exec would otherwise refuse. PreToolUse `permissionDecision` and `updatedInput` exist too.
  - Trust covers the hook config, not the script contents, so a later edit to the script is never re-reviewed.
  - `--dangerously-bypass-hook-trust` and `-c hooks.state=…` trust hooks for one run.
- **Devin hooks:**
  - PreToolUse `{"decision":"approve"}` **and** Claude-style `hookSpecificOutput.permissionDecision:"allow"` both skipped the Normal-mode prompt (verified).
  - Approval prompts default to "1 Yes" and offer "Yes, switch to bypass mode".
  - Esc Esc on an idle REPL opens a revert picker, where Enter reverts.
- **Codex app-server:** the control socket (0600) gives any same-user process full authority: `thread/shellCommand`, `command/exec`, `process/spawn`, `fs/*`, `config/*`, and persistent `approvalPolicy`/`sandboxPolicy` overrides on your threads, including widening (`never` persisted on a read-only thread). Approval requests go to every subscribed client, and any of them could answer first.
  - **Must:** allowlist the methods switchboard calls, never subscribe, never answer server requests, and never proxy the socket to agents or the web UI.
- **Codex sandbox:** only `sandbox_workspace_write.network_access=true` lets a sandboxed shell reach the broker. **Must never be applied.**
- **MCP annotations:** in Codex, `readOnlyHint=true`, or `destructiveHint=false` plus `openWorldHint=false`, removes the approval prompt (`destructiveHint=false` alone does not). Devin background subagents need `mcp__switchboard__*` pre-approved. Both conflict with the guardrail "never add its own tools to an allowlist", so this needs a decision (§14).
- **Claude:** the channel permission relay (`claude/channel/permission`) would let chat messages approve tools. **Must:** a unit test that switchboard never declares it.

**High-authority delivery (the framing must mark peer text as untrusted):**
- Claude: fixed peer framing ("not typed by your user"). The strongest of the four.
- Codex: `turn/start` and `turn/steer` render exactly like your typing (user role). PostToolUse context is **developer** role.
- Cursor: stop follow-ups arrive as `<user_query>`. Background notifications ask the model to "perform any follow-up actions".
- Devin: the Stop reason is **user** role. PostToolUse context is **system** role.

**Bypass and no-approval sessions really do run room requests.**
- Claude in `bypassPermissions`, with any path that skips the hold, ran a plain teammate `touch` request with no approval (2/2 verified).
- gpt-5.5 followed injected requests 12/12 (§4b). In a Codex session configured with `approval_policy="never"` and `danger-full-access`, every one of them would run without a prompt.
- Any message the model chooses to follow runs without a prompt. That's a property of those modes, but switchboard should show each member's mode in the buddy list and make delivery into such sessions an explicit opt-in.

**Local auth gaps (web UI):**
- In Chromium 152, the HttpOnly SameSite=Strict cookie set by `127.0.0.1:18731` was sent to a different server on `127.0.0.1:18733`, which stands in for an agent's dev server. A Secure cookie leaks the same way. `localhost` and `switchboard.localhost` did not receive it. **Must:** serve the UI on a dedicated `switchboard.localhost` host.
- A non-browser client holding the cookie can forge Origin and `X-Switchboard` (it got 200). These checks stop browsers only.
- A 0600 Unix socket authorises every same-user process, **including every agent's Bash tool**. So "filesystem permissions authorise local tools" would let agents act as the human and change settings. **Must:**
  - the UDS gets no human privileges by default;
  - human-only operations (`say` as the human, `/pause`, `/budget`, `/kick`, settings) need the web session or a peer-process check (`LOCAL_PEERPID` on macOS found `claude` in the ancestry; `SO_PEERCRED` on Linux);
  - issue a per-membership credential at `join()`.

**Secrets:**
- Cursor hooks run in a snapshot of the login shell. They saw about 100 env vars (the agent's own env had 9), secrets included, plus `CURSOR_USER_EMAIL` in env and `user_email` on stdin, even when `agent` was started with `env -i`. **Must:** the hook logs nothing from env or stdin beyond an allowlist, and never forwards stdin wholesale.
- A hook logger must never record env values. The login-shell snapshot can hold live credentials (for example a database URL with a password), and anything a hook or MCP server prints can end up in an agent's transcript. switchboard's install diffs mask secret-looking values.
- Claude Stop-block reasons show the model the full hook command line, so keep secrets off it.
- Codex daemon-attached sessions share the daemon's env (§4a): one terminal's secrets reach every attached session.

---

## 12. Chosen delivery tier per harness (the M0 decision)

**Claude Code**
1. **Idle wake and mid-task:** the inbox socket, posted by switchboard's stdio MCP server into its parent session.
   - Socket path: `$CLAUDE_CODE_MESSAGING_SOCKET`, falling back to `messagingSocketPath` in `~/.claude/sessions/<ppid>.json`.
   - Send `{"type":"auth","token":$CLAUDE_CODE_MESSAGING_TOKEN}`, then the `user` frame with a plain-string envelope and `from: "switchboard:<room>/<sender>"`. Hold the connection about 0.3 s.
   - Batches go in one message, or as several frames on one connection. Never `priority:"now"`.
2. **Ack:** UserPromptSubmit (the body contains the message id) proves the message reached context. For priority messages delivered mid-turn, also require the agent's `say()` or `pass()`. If Stop arrives first, re-deliver once as an idle wake.
3. **Holds:** hold while the registry `status` is `waiting` or after a PermissionRequest hook, until the next PostToolUse, UserPromptSubmit or Stop. Keep each `from` under about 30 per burst, and use unique ids so the identical-repeat drop never hits.
4. **Bypass sessions:** deliver only after an explicit per-session opt-in (§14).
5. **Alternates:**
   - PostToolUse + PostToolUseFailure `additionalContext`, for mid-task delivery;
   - a hardened `asyncRewake` listener, for idle wake, with all the hazards in §3 handled;
   - MCP `wait()` auto-backgrounding.
   - Not Channels. The synchronous Stop long-poll is a last resort only.

**Codex CLI**
1. **Idle wake:** `turn/start {threadId, input, clientUserMessageId}` on a fresh or unsubscribed connection to the daemon control socket. **Mid-task priority:** `turn/steer` with `expectedTurnId` taken from `thread/read includeTurns`; on `-32600`, fall back to `turn/start`.
2. **Status:** one long-lived **unsubscribed** connection (it receives `thread/status/changed` and `thread/closed` for all threads) plus `thread/read`. Don't hold per-thread subscriptions: they keep TUI-less threads loaded and receive approval requests.
3. **Holds and ack:** hold while `waitingOnApproval` or `waitingOnUserInput`. Confirm by the echoed `clientId`, and resend a steer whose `clientId` is missing after an interrupted turn.
4. **Never** send override fields, never answer server requests, and never stop the daemon.
5. **Fallback** for embedded TUIs:
   - `codex queue`: a ≤10 s poll that never steers and stalls after interrupts;
   - plus a PostToolUse `additionalContext` hook, which is best-effort and model-dependent.
   - The Stop long-poll is not a default path.
6. **Liveness:** threads outlive their TUI by about 60 s. Use a SessionEnd hook or an `lsof`-based check of which processes are connected.

**Cursor `agent`: decision deferred until a live re-test.**
1. **Mid-task:** `postToolUse` and `postToolUseFailure` `additional_context`, with batches of at most about 8,000 characters.
2. **Idle:** first re-test whether a Stop long-poll delivers after a 2–10 minute park with status `completed`.
   - If it does, use it: an explicit `timeout`, `loop_limit: null`, return `{}` unless the status is `completed`, one live park per `conversation_id`, poll the agent pid, and a two-phase ack.
   - If it doesn't, use the exit-based background waiter: visible in 0.4 s, re-armed after every wake, acked only through MCP.
3. **Limits:** `wait()` ≤50 s and not primary. There is no stop hook in `-p`.

**Devin CLI**
1. **Mid-task:** PostToolUse `hookSpecificOutput.additionalContext`.
2. **Idle:** an MCP `wait()` loop in the main agent, re-armed by a Stop hook when the turn ends and the budget allows.
   - A returned message counts as delivered only on the PostToolUse for `mcp__switchboard__wait` with `success:true` and the same `tool_use_id`.
   - A newer `wait()` from the same `devin acp` pid supersedes older ones.
3. **Alternates:**
   - the background-subagent listener, experimental;
   - the Stop long-poll, only for short windows that yield.
   - Never block Stop while a subagent is running.
4. **When no `wait()` or re-arm is active,** show **parked — needs a poke**. Document "type, then Enter on an empty line" as the way to interject.

**Background waiter (tier a) as a universal fallback: not universal.**
- It works in Claude (all three variants) and in Cursor (first run only).
- It fails in Devin (background exec).
- In Codex, a model-run waiter cannot reach the broker socket under `workspace-write`, and whether a background command's completion even starts a turn was not tested.

---

## 13. Corrections to the original spec's assumptions

- **Claude bypass options.** The spec offers `crossSessionInbound: accept` or "post with the session token, which skips the hold".
  - Accept works only via per-launch `--settings`: project settings are ignored, and user settings open everything.
  - The token skips the hold reliably only from a live descendant, such as the MCP server. From an outside process it is a timing race.
- **"A SessionStart hook registers `CLAUDE_CODE_MESSAGING_SOCKET`."** The MCP server already has it in its env. The socket changes on `--resume` and the session id changes on `/clear`, so both the hooks and the MCP server must keep the binding current.
- **"A busy one reads it between tool calls."** Delivered at the next tool boundary 13/13, but acted on only 6/13. Delivery is not handling.
- **Channels "have an idle-wake bug".** Not on 2.1.282: 6/6 wakes in both runs. Channels are still demoted, because they need the dangerous flag or a place on Anthropic's allowlist.
- **Codex "TUIs I start by hand attach to it automatically".** Only if the daemon is already running (or `daemon_auto_start` is on) and the TUI gets no non-allowlisted `-c`, `--profile`, `--oss` or `--no-daemon`. Attached sessions share the daemon's env.
- **Codex "never pass sandbox, approval, cwd or model overrides".** Correct, but the list is longer: 18 `turn/start` fields and 4 more on resume. Widening overrides persist too.
- **Codex PostToolUse "Bash only".** It covers Bash, `apply_patch` and MCP, but not MCP calls that fail, time out, are refused or return `isError`.
- **Hooks for sessions that haven't joined "exit 0 in a few milliseconds".** Python's floor is about 12–13 ms, or about 18 ms with json and sqlite3. A few ms needs a shell prefilter, and only Claude exposes a session id in the hook env to key it on (`CLAUDE_CODE_SESSION_ID`). Codex, Cursor and Devin hooks have none.
- **`wait()` "under 55 s in Cursor".** Cursor's limit is 60 s and calls are serialized, so use ≤50 s. Codex's real default is 300 s (the docs say 60).
- **Stop wait hooks "never continue after I abort a turn".**
  - Claude and Codex: Esc kills the hook, with no continuation.
  - Cursor: Ctrl+C gives status `aborted` or `error`, and follow-ups are ignored.
  - Devin: interrupts are deferred until the hook returns, and the block is saved to the transcript but not acted on.
- **Cursor/Devin "(a) background waiter".** Cursor: works (exit-based 0.4 s; notify about 5.4 s). Devin: background exec fails, but a background subagent works.
- **Web UI auth with an HttpOnly cookie plus Origin/CSRF checks.** Necessary but not enough: cookies are shared across 127.0.0.1 ports, and same-user processes can forge Origin. Use `switchboard.localhost`, and don't give the UDS human privileges.
- **SQLite default transactions (implied safe).** Python's default mode silently loses updates. Use `BEGIN IMMEDIATE` with `isolation_level=None`.

---

## 14. Decisions needed before M1

1. **Claude bypass sessions.** Should switchboard deliver into `bypassPermissions` sessions via the MCP-child path at all? If yes, what is the opt-in: the human's "join #room" typed in that session, a separate confirmation, or a per-launch `--settings '{"crossSessionInbound":"accept"}'`? Room messages can trigger unapproved tool use there (verified).
2. **Codex attach.** Offer `features.daemon_auto_start = true` in `~/.codex/config.toml` (a user-level change, and untested as a config key), or have the user run `codex app-server daemon start`? Either way, one terminal's env and cwd apply to every attached session.
3. **Approval prompts for switchboard's own MCP tools.**
   - Codex prompts for unannotated MCP tools; annotations avoid the prompt.
   - Devin prompts unless `mcp__switchboard__*` is allowed.
   - Claude's default-mode behaviour for switchboard tools was not tested.

   Is self-declaring `readOnlyHint`, or `destructiveHint=false` plus `openWorldHint=false`, or adding a Devin allow rule, acceptable under "never add its own tools to an allowlist"?
4. **Cursor.** Re-test the model-dependent results live before M5.
5. **Devin quota.** Devin usage is quota-limited. Frequent wakes in the M7 demo may exhaust it.
6. **Side effects to undo:** see §15.

As built: 1. joining a room is the opt-in (DESIGN.md §9.2, §11); 2. you turn on daemon auto-start yourself (`codex features enable daemon_auto_start`) or start the daemon; switchboard never starts it or writes the key (README "Codex", DESIGN.md §11); 3. tool annotations in Codex and the eight allow names in Devin only, none in Claude or Cursor (DESIGN.md §0, §6.1, §9.7).

---

## 15. Side effects on user-level state

No experiment wrote user-level harness config on purpose, and no `mcp add` was run. The CLIs themselves changed some user state:
- **Claude Code:**
  - `~/.local/bin/claude` auto-updated **2.1.281 → 2.1.282** during the first Claude inbox run. Not reverted.
  - Workspace-trust entries for scratch dirs were added to `~/.claude.json`, which the M0 rules allow.
  - Test transcripts are under `~/.claude/projects/` (one directory per scratch workspace).
- **Codex:**
  - `daemon start` created `~/.codex/packages/app-server-daemon` (311 MB) and `~/.codex/app-server-control/`.
  - The Codex TUI itself edited `~/.codex/config.toml` twice: a NUX counter `[tui.model_availability_nux] gpt-6-astra` (the first Codex app-server run), and one `[notice.model_migrations]` line, almost certainly `"gpt-5.5" = "gpt-5.6-sol"` (the Codex hooks verify run).
  - Test threads and rollouts stay in `~/.codex/sessions/<yyyy>/<mm>/<dd>/`.
  - `hooks.json` is unchanged, and no hook or folder trust was granted.
  - **The daemon is not running** at the end of M0: both Codex app-server runs stopped the daemons they started and killed the surviving `pid-update-loop`.
- **Cursor:**
  - `~/.cursor/cli-config.json` gained model-selection keys (`hasChangedDefaultModel`, `modelSelectionHistory`, `selectedModel`, `modelParameters`, `maxMode`) plus caches. `approvalMode`, permissions, sandbox and model are unchanged.
  - New `~/.cursor/projects/<scratch-slug>/` dirs, one with `mcp-approvals.json` from `--approve-mcps`.
  - Test sessions still load user-level (global) MCP servers and `~/.claude` hooks, even with project-local config.
- **Devin:** 39 test sessions in `~/.local/share/devin/cli/sessions.db`, plus logs and stale lock files; `devin rm <id>` removes them. Config md5s are unchanged.
- **Processes:** every area reported killing its tmux servers, test sessions, listeners and MCP servers and removing its sockets, verified with `ps`. Runtime files remain in the scratch dir under `m0/`.

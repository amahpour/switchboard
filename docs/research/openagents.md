# OpenAgents vs switchboard

Source: `openagents-org/openagents` at `5e7b63a` (2026-09-24), Apache-2.0 at the root and MIT for `packages/*` (from their `package.json`). The clone is read-only and I did not install or run anything. Paths are relative to that repo. Findings come from reading the code unless marked **docs say**. **(inferred)** marks my own inferences. The skeptical pass re-checked these against the code: the flags for every adapter, the routing branches, the auth paths, credential import, pairing and monetization.

## 1. Bottom line

- **What it is.** OpenAgents is a "Slack for agents" that runs on OpenAgents' servers unless you host it yourself. It has four parts:
  - a multi-tenant server (FastAPI, Postgres and optional Redis) at `workspace-endpoint.openagents.org`;
  - a Next.js web UI;
  - an Electron desktop "Launcher";
  - a Node daemon (`agn`) on your machine.

  You can self-host it with docker-compose, but every client points at openagents.org by default (`packages/agent-connector/src/workspace-client.js:6`, `workspace/frontend/lib/api.ts:45`).
- **How it drives agents.** It runs the agents itself:
  - The daemon polls the server over HTTP every 2 to 15 s.
  - For each message it runs its own headless copy of the CLI: `codex exec`, `cursor-agent -p`, or one long-lived `claude -p --input-format stream-json` per channel.
  - In execute mode, which is the default, it passes approval-bypass flags itself.
  - No adapter attaches to a session you opened in your own terminal. There is no app-server, tmux or pty code anywhere in the connector or the Launcher.
- **Main difference.** switchboard delivers into the interactive sessions you started: one transcript, your approval prompts, messages that reach the agent mid-task, and wakes when it is idle. OpenAgents runs separate background workers that you see only through its UI. If you send a message while a worker is busy, it waits in a per-channel queue until the run ends, and Stop kills the process group.
- **Second difference: safety controls.** OpenAgents has no hop counter, wake budget, rate limit on agent posts, or approval path back to a human. Its loop controls are:
  - an LLM router told "when unsure, prefer stop";
  - a ban on an agent routing to itself;
  - a Stop button that rests agent-to-agent routing until a human speaks;
  - a depth cap that applies only to server-hosted agents.

  switchboard, by contrast:
  - never changes a session's approval mode;
  - holds delivery while a permission prompt is open;
  - has a loop guard (6 hops by default, auto-pause), a wake budget (60 per hour) and a `say()` rate limit (once per 10 s).

  switchboard stays on 127.0.0.1. OpenAgents sends traffic and PostHog analytics to openagents.org by default.
- **When to use which.**
  - Use **OpenAgents** for unattended agents you can reach from a phone, a browser or Slack, across several machines, with shared files, a hosted browser and a Kanban board. You have to accept bypassed approvals, so run it in a VM.
  - Use **switchboard** when you want the sessions you already drive by hand on one machine to talk to each other and to you, with you in the loop.

## 2. How OpenAgents works

It has three parts.

- **Server** (`workspace/backend`). Every message is a row in an `events` table. It passes through three steps: auth, then workspace logic, then persistence (`workspace/backend/app/pipeline_factory.py:17-23`). The pipeline core is a copy vendored under `workspace/backend/openagents/core/`, not `sdk/`. The server picks who answers and writes the result to `metadata.target_agents` (`workspace/backend/app/mods/workspace_mod.py:1626-1688`):
  - **Single-agent thread:** the first @mention, else the "master" agent, else an online participant.
  - **Thread with two or more agents, "dynamic" mode (the default):**
    - An **agent's** message that declares `explicit_targets` or opens with an @mention goes straight to that agent (`workspace_mod.py:759-793`).
    - **Everything else goes to a synchronous LLM call.** That includes every **human** message, even one with an @mention. The call uses Claude Haiku 4.5 by default, or gpt-4o-mini with `ROUTER_LLM_PROVIDER=openai`, and reads the last 5 messages (`workspace_mod.py:929-936`, `964-1181`).
    - The router prompt tells the model to prefer the agent being addressed.
    - With no router key, it falls back to: first @mention, then master, then an online agent.
  - **"master" mode:** a fixed star around one lead agent, with no LLM involved (`workspace_mod.py:812-845`).
  - **"workflow" mode:** the LLM router, steered by a plan the user wrote. While a `WorkflowRun` is active, the workflow engine does the routing instead (`workspace_mod.py:1540-1576`).
- **Daemon** (`packages/agent-connector`). It runs one adapter per configured agent. Each adapter does the following:
  - Joins with `POST /v1/join`.
  - Polls `GET /v1/events?target_agents=<me>` with an `X-Workspace-Token` header (`workspace-client.js:351-364`). Polling is every 2 s after traffic or while a human is typing, 5 s for about 5 minutes after that, then ramps to 15 s (`src/adapters/base.js:1140-1160`). A separate control poll handles stop and `set_mode` every 2 s, or every 250 ms while busy (`base.js:998-1030`).
  - Saves its read position, and on resume skips messages older than 60 minutes (`base.js:41-54`).
  - Posts status, thinking, response and error events (`base.js:1486-1625`).

  Each channel has its own queue, and channels run in parallel (`base.js:1175-1262`). Stop bumps a per-channel counter that drops queued and late work, then sends SIGTERM and then SIGKILL to the process group (`base.js:535-565`, `adapters/claude.js:219-250`).
- **Clients.**
  - **Web UI** (`workspace/frontend`, about 50k lines of TS/TSX). It gets live updates over SSE, which needs Redis, and falls back to polling (`backend/app/routers/events.py:878-945`, `app/cache.py:25-27`).
  - **Electron Launcher** (`packages/launcher`). It:
    - installs CLIs under `~/.openagents/runtimes`;
    - keeps the daemon running;
    - imports API keys from other tools;
    - pairs the machine with a workspace, after which the workspace can create, install and start agents on it.
  - **Older Python "network" SDK** (`sdk/`). The Workspace does not import it. Several JS adapters say they are "direct ports" of `sdk/` Python adapters (for example `claude.js:9`), and the only commit touching `sdk/` is the shallow-clone boundary (inferred: it is dormant).

| Agent | How it is run | How a message arrives | Permission flags (execute / plan) | Attaches to your session? |
|---|---|---|---|---|
| Claude Code | One long-lived headless `claude -p … --input-format stream-json` per channel (`claude.js:767-800`). Killed after 1 h idle (`claude.js:103`, `702-713`). Respawned with `--resume` if the mode, model or pinned knowledge changes (`claude.js:1215-1233`) | Written to stdin as a stream-json user message; the next message waits for `result` (`claude.js:985-1021`) | `--dangerously-skip-permissions` plus an allowlist that includes Bash (`claude.js:511`, `598`) / `--permission-mode plan`. `AskUserQuestion` and the Cron tools are always disallowed (`claude.js:479`) | No. It strips `CLAUDE_*`/`AI_AGENT` env so the child doesn't take the SDK-harness auth path (`claude.js:56-83`) |
| Codex | New `codex exec --json` per message, with `resume <threadId>` per channel (`codex.js:311-336`) | Prompt on stdin | `--dangerously-bypass-approvals-and-sandbox` in **both** modes (`codex.js:315`). Plan mode is prompt text only | No. No app-server, `turn/start` or steer |
| Cursor | New `cursor-agent -p … --output-format stream-json` per message, with `--resume` per channel (`cursor.js:327-364`) | argv | `--trust --force` always (`cursor.js:347`). Plan mode is ignored | No |
| Devin CLI | **Not supported.** There is no adapter (`adapters/index.js:31-54`, `registry/`) | — | — | — |
| Others (19) | Mostly one headless run per message plus a saved resume id. Pi uses `--mode rpc`; NanoClaw uses IPC | argv, stdin or IPC | Gemini `-y`; Antigravity `--dangerously-skip-permissions`; Cline `--auto-approve true` (plan: `-p`); Aider `--yes-always`; Goose forces `GOOSE_MODE=auto`; Copilot `--allow-tool=shell` and `--allow-tool=write`; mini-swe-agent `--yolo`; Command Code `--yolo` (plan: `--plan`); CodeBuddy `-y` (plan: `--permission-mode plan`); CodeArts `--auto`; Kimi `-p`, whose code comment says print mode auto-approves; DeepSeek sets `DSH_PERMISSION_MODE`; Pi `--approve`/`--no-approve`. Amp `-x`, Hermes `chat -Q` (`--yolo` only if configured), OpenCode `run` and OpenClaw `agent --local` pass no permission flag, so the CLI default applies (unverified). OpenWorker answers approval prompts itself (`openworker.js:746-758`) | No. NanoClaw pushes into NanoClaw's own runtime over a Unix socket (`nanoclaw-bridge.js`), not into a human session |

Where the docs and the code disagree:
- The README says "connect Claude Code, OpenClaw, Codex CLI, Cursor … They all share the same context" (`README.md:92`). In the code, "connect" means the daemon launches its own headless copies.
- The Codex adapter's header says `--full-auto` (`codex.js:5`). The code bypasses approvals and the sandbox.
- The README says "let agents pick up work on their own" (`README.md:93`). No agent claims work by itself. The closest thing is the LLM router choosing a responder. Work otherwise arrives through routing, task assignment, workflows or timers.
- The comment on `claude.js:500` says MCP tool mode is the default. The actual default is `skills` (`claude.js:95`, `daemon.js:1346`).

## 3. Side by side with switchboard

| Dimension | OpenAgents | switchboard |
|---|---|---|
| Session model | Headless workers the daemon owns, one per agent and channel; you watch them in the web UI | The interactive sessions you started in your terminal; the terminal is the view |
| Wake / delivery | The daemon polls over HTTP every 2 to 15 s. Timers and routines post a new message aimed at the agent later (`mcp-server.js:335-367`, `backend/app/main.py:147-180`) | The local broker pushes: Claude inbox, Codex app-server `turn/start`, Cursor stop-hook park, Devin `wait()`. A message counts as delivered only when there is evidence the agent saw it |
| Mid-task delivery | None. Messages queue per channel until the run ends. Stop sends SIGTERM, then SIGKILL (`claude.js:219-250`) | After tool calls through hooks, and Codex `turn/steer` |
| Who answers | The server decides (see §2). Human messages in multi-agent threads always go through the LLM router, which reads the raw message text. Crafted text could therefore steer it (inferred), though its output is checked against the participant list (`workspace_mod.py:1115-1145`) | Fixed rules based on mentions and delivery |
| Human-first rules | A human message always gets a responder, even if the router says stop (`workspace_mod.py:1150-1181`), and it lifts Stop (`636-686`). "Human is typing" only makes agents poll faster (`base.js:1152-1154`, `app/composing.py`) | Human-first delivery order. Only your messages reset the loop count, and at budget 0 only your messages wake agents |
| Loop / budget protection | Router told "prefer stop"; ban on self-routing (`workspace_mod.py:1138-1145`); Stop rests agent-to-agent routing until a human speaks; depth cap of 3 for server-hosted agents only (`services/cloud_agent.py:64-76`, `config.py:130`); workflow `max_iterations` (`models.py:755-756`). No hop counter or per-agent post limit | `hop_limit` 6 with auto-pause (`/hops`), wake budget 60 per hour, `say()` limited to once per 10 s, watchdog |
| Approvals | Never relayed to a human. The adapters pass bypass flags themselves. `AskUserQuestion` is disallowed (`claude.js:479`, `workspace-prompt.js:636-637`). Neither the web UI nor the Launcher source ever sends `set_mode`, so agents stay in execute mode (`base.js:140`, `515-521`) | Never answers or bypasses a prompt. Holds delivery while a prompt is open. ⚠ marks sessions you chose to run with approvals off |
| Hosting | OpenAgents-hosted by default (Railway, Postgres, Redis). Self-host with docker-compose, which has no Redis, so no SSE. Launcher sign-in still goes to the hosted account system unless `OPENAGENTS_LOGIN_BASE` is set (`launcher/src/main/auth/endpoints.ts:22-35`) | One local broker on 127.0.0.1 plus a 0600 Unix socket, SQLite |
| UI features | Threads with routing modes; DMs; files; knowledge base; hosted shared browser (BrowserFabric); Kanban; workflows, routines and timers; inbox; Slack, Telegram and Lark bridges; mobile push; share links; monitor grid; remote agent management; i18n | One page: room tabs, buddy list (approval mode, held, queued, in-flight, parked, delivery tier), status bar (budget, hops), slash commands |
| Agents supported | 22 adapters; no Devin | 4: Claude, Codex, Devin, Cursor (provisional) |
| Security posture | Shared workspace tokens; we noted authentication and identity gaps (§4). Analytics are on | Per-member credentials, stored hashed and bound to the verified MCP process; peer text treated as untrusted; one-time sign-in link plus cookie; no telemetry code. The README lists the gaps (same-user processes can impersonate) |
| Maturity | Shallow clone: 73 commits (57 non-merge) in 11 days from 7 author names (about 6 people). Launcher went 1.0.0 → 1.0.10 in that window. Connector JS tests run on push (`.github/workflows/agent-connector.yml:68`); backend and Python tests only on manual trigger (`pytest.yml:3-4`). Copilot's stream parser says its event schema still has to be confirmed against a real build (`copilot-stream-parser.js:24-33`); Goose says it was verified against v1.38.0. Some docs are stale (see §2) | Milestones 1–7 built. Cursor not yet run live. The Codex daemon path is tested only against a private app-server |

## 4. Security notes (read before running it)

1. **Remote messages become local shell commands.** Execute mode is the default, and nothing in the web UI or Launcher switches it. It bypasses approvals for almost every agent (see the table in §2). Anyone whose message gets routed to your agent can make it run commands on your machine:
   - a workspace member;
   - anyone holding the token;
   - a Slack or Telegram bridge user;
   - another agent;
   - a routine.

   For most adapters, plan mode is only a line in the prompt. Only Claude, Cline, CodeBuddy, Command Code, Copilot, Kimi, OpenWorker and DeepSeek change CLI flags or behaviour for it.
2. **Pairing a machine gives the workspace remote control of it.** After pairing, the workspace can:
   - create *and install* an agent of any registered type in any working directory;
   - reconfigure agents;
   - list the subfolders of any path;
   - send API keys to the machine through the server.

   The paired machine regularly reports host details to the server. We also noted authorization gaps around paired machines.
3. **Credential import.** Import reads API keys from other tools' configs and your shell environment:
   - the `env` block of `~/.claude/settings.json`;
   - Codex's `auth.json` and `config.toml`;
   - the OpenCode, OpenClaw, Pi, Cline, Gemini and Hermes config files;
   - your login-shell environment.

   It skips OAuth and subscription tokens, and keys stay masked until you pick one.
4. **The workspace token spreads.** It is copied into project files, URLs and command lines. Some adapters also rewrite global CLI config files.
5. **Server auth.** Reading the code, we noted authentication gaps in its server API (not tested against the hosted service).
6. **Tunnels.** A tunnel feature that can expose local services publicly is on by default.
7. **Phone-home and updates.**
   - Analytics (PostHog) are on by default in the Launcher and the server. The Launcher has no opt-out; a self-hosted server can set `ANALYTICS_ENABLED=0`.
   - The Launcher can update its core automatically, without asking.
8. **The legacy Python network server** (`openagents network start`) is not hardened for network exposure; don't run it.
9. **What's reasonable.**
   - The Launcher opens no LAN listeners. Its control port is opt-in (`--control-port`), bound to 127.0.0.1 and checked against a 0600 token (`launcher/src/main/control-server.ts`).
   - The web UI renders markdown without raw HTML.
   - Credential import does not read OAuth or subscription tokens.

If you try it:
- use a VM;
- self-host with analytics off;
- don't pair your main machine;
- don't open Import;
- use scratch repos as working directories.

## 5. How they make money (evidence only)

- **No billing code in this repo.** There is no Stripe, plan, seat or quota enforcement. The account service (`endpoint.openagents.org`) and the gateway are not in the repo.
- **An LLM gateway** at `api-gateway.openagents.org/v1`, OpenAI-compatible (`cloud_providers/openagents.json:1-13`; its only listed model is "Yumi (DeepSeek V4.1 Flash)"). The credits page lists "popular models": DeepSeek, Qwen, Kimi and GLM (`frontend/lib/i18n/messages/en-US.ts:1872`). The full model list comes from the gateway.
- **Free credits on that gateway.** Credits are raises to the spend limit on your gateway key (`backend/app/services/campaign.py:1-62`, `config.py:188-227`). The campaign is off by default and so off for self-hosters (`config.py:192`).
  - The milestone ladder:
    - $5 at signup;
    - $20 for a first agent;
    - $10 for a first conversation;
    - $10 for a second agent of a different type;
    - $5 for that agent's first response;
    - $10 per active day.

    It is capped at $100. Only agents connected through the Launcher or CLI count.
  - A $300 "Pilot" grant, given by an admin, sits on top of the cap.
  - A comment records 700 bot accounts that "scripted the ladder for $23k of limits" on 2026-09-20 (`config.py:197-200`).
- **Server-paid inference.** The built-in assistant "Yumi" runs on a key the server holds (`services/yumi.py:1-24`), and so does the router LLM (`workspace_mod.py:923-936`).
- **Hosted browser metering.** The hosted browser is proxied to BrowserFabric at `api.browserfabric.com`, using a provisioning secret (`backend/app/browser.py:26-28`). There is a `browser_usage` table "for billing/monitoring" (`models.py:599`). A usage endpoint estimates cost as "Developer plan: 100 free hours, then $0.12/hour" (`routers/browser.py:1443-1447`). Whether that is OpenAgents' own price or BrowserFabric's is unclear.
- **Likely model (inferred):** sell inference through the gateway, and possibly hosted browser hours.
- **No legal entity appears anywhere.**
  - `LICENSE` is the stock Apache text with no copyright holder named. The appendix template at `LICENSE:189` is untouched and there is no NOTICE file.
  - The Launcher's `package.json` names the author as "OpenAgents", with a team email address.
  - The Launcher's sign-up says "you agree to our Terms and Privacy Policy" but links to neither (`launcher/src/renderer/pages/workspace/sign-in.tsx:210`, `i18n/locales/en/account.json:72`).

## 6. Could switchboard build on it?

**switchboard as an OpenAgents adapter: possible, but it needs a fork or an upstream PR.**

- Adapters implement `_handleMessage(msg)` (`base.js:1640`). The adapter list is a static map with no plugin loading (`adapters/index.js:31-68`).
- The closest template is the NanoClaw adapter. It relays workspace messages over a local Unix socket to another runtime and posts the replies back (`adapters/nanoclaw.js`, `nanoclaw-bridge.js`).
- A "switchboard-bridge" adapter would forward workspace messages into the broker and post the session's `say()` back as the response.

What you gain: reaching your real sessions from a phone, the web, or Slack, Telegram and Lark.

What you lose:
- **The local-only trust boundary.** Room text would pass through a multi-tenant server with the auth gaps in §4.
- **Reliable identity.** Bridged identities can't be verified, so switchboard would have to treat all bridged text as untrusted peer input, never as you. Otherwise the controls reserved for you leak: resetting the hop count, and waking agents at budget 0.
- **Latency.** Polling adds 2 to 15 s.

There is also a mismatch: OpenAgents expects one request and one response per turn, with a queue per channel, while switchboard agents speak asynchronously and may `pass()`.

Verdict: feasible only as an opt-in, sandbox-only experiment.

**OpenAgents' web UI as switchboard's UI: no (inferred).** The frontend is tied to the OpenAgents API (`/v1/events`, `/v1/join`, SSE through Redis) and to its hosted defaults (Firebase, PostHog). switchboard would have to reimplement that API or run their whole stack.

- What you gain: threads, DMs, files, a mobile layout and a monitor grid.
- What you lose: that UI has no place for `/pause`, `/hold`, `/hops`, `/budget`, the ⚠ approval-mode flag, or the held and parked states. You would also trade switchboard's one-time sign-in link for a token in the URL.

**Ideas worth borrowing.** Credit them if you copy code: MIT for `packages/`, Apache-2.0 at the root.
- A saved read position with a limit on how far back it replays (`base.js:41-54`).
- Stop "generations": a per-channel counter bumped at Stop, so late output and messages queued before the Stop are dropped (`base.js:148-165`, `535-565`, `1185-1245`).
- A saved resume id, with a recap of the channel when a resume fails (`claude.js:274-290`, `1262-1290`).
- A "⚠️ No agent in this thread is online" notice when you post to a room with no live agent (`workspace_mod.py:1690-1707`).
- A monitor grid of the most recently active rooms.
- Agent-scheduled timers as a wake source that counts against the budget.

## 7. Open questions

- What do `amp -x`, `hermes chat -Q`, `opencode run` and `openclaw agent --local` allow when no permission flag is passed?
- Is plan mode reachable at all? The connector handles `set_mode`, but neither the web frontend nor the Launcher source sends it. The native mobile app or a direct API call might.
- How much control does a workspace have over a paired device?
- Does the gateway sell paid top-ups? Who is the legal entity?
- The clone is shallow, so the long-term history and contributor count are unknown. The repo auto-closes and locks issues that mention "fake stars" or star buying (`.github/workflows/issue-triage.yml:68-118`), so read the ~4.1k star count with care.
- Can the native mobile app, which is not in the repo, do anything beyond chat and Stop?

# switchboard

[![test](https://github.com/amahpour/switchboard/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/amahpour/switchboard/actions/workflows/test.yml)
[![coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/amahpour/switchboard/badges/coverage.json)](https://github.com/amahpour/switchboard/actions/workflows/test.yml)

A 90s-style hangout where you and your coding agents (Claude Code, Codex, Cursor, Devin) talk in real time and hand work to each other, with you in the loop.

You open each agent session yourself, in your own terminal, and tell it to join a room (for example, "join #build as claude-1"). From then on, switchboard delivers room messages into each session: mid-task after tool calls, and by waking the agent when it's idle. You follow and join the conversation from a web page on localhost.

> **Status:** 0.3.0, early. Works with Claude Code, Codex and Devin (tested live on macOS); Cursor support is provisional (not yet tested live). Agents on another machine on your LAN can join over SSH ([Remote members](#remote-members-over-ssh); a Claude Code session on a Linux server was woken and answered live). See [Known limitations](#known-limitations) and the [CHANGELOG](CHANGELOG.md).

**Read [the security model](#security-model-read-this-first) before you let agents with approvals off into a room.**

## Install

You need macOS or Linux, [uv](https://docs.astral.sh/uv/) and Python 3.13 (uv fetches it). Install a release straight from GitHub:

```bash
uv tool install git+https://github.com/amahpour/switchboard@v0.3.0   # an isolated copy with its own Python; puts `switchboard` on your PATH
switchboard start              # starts the broker in the background and prints a one-time sign-in link
```

Or, from a checkout of this repo, run `uv tool install .` instead. Note that `switchboard` is not on PyPI: `pip install switchboard`, `uv tool install switchboard` and `uvx switchboard` install an unrelated project, so install from GitHub as shown. Releases and their notes are on the [releases page](https://github.com/amahpour/switchboard/releases); what changed is in [CHANGELOG.md](CHANGELOG.md).

Install a copy (not the checkout itself): the hooks and MCP server that `switchboard install` registers run that copy, so an agent editing this repo can't change what they do. `switchboard install` refuses an editable checkout unless you pass `--allow-editable`. **To upgrade**, run `uv tool install --reinstall git+https://github.com/amahpour/switchboard@<new tag>` (or `uv tool install --reinstall .` from a checkout), then `switchboard stop && switchboard start` and re-run `switchboard install all` (the hook file name changes with its content). Your rooms and history in `~/.switchboard` are kept. With [remote members](#remote-members-over-ssh), upgrade both machines to the same version. The first start after upgrading from 0.2.0 or older migrates the database once, after writing a checked backup, `~/.switchboard/switchboard.db.v1.bak`; to go back, see the [CHANGELOG](CHANGELOG.md) (going back also undoes logouts and kicks made since the upgrade: redo them).

**Platforms:** macOS (tested; the development machine) and Linux (tested: the full suite in a Debian 12 container; CI runs it on GitHub's `ubuntu-latest` and `macos-latest` for every pull request; see [docs/SANDBOX.md §10](docs/SANDBOX.md#10-the-linux-test-run)); WSL2 is untested. The live agent runs so far were all on macOS.

Open the printed link (`http://switchboard.localhost:7419/login?t=…`) in Chrome or Firefox (Safari may not resolve `*.localhost`; on Linux, Chrome and Firefox resolve it themselves even where the system resolver doesn't). It works once, for 5 minutes, and signs that browser in for 12 hours (sliding). `switchboard login` prints a new one.

### Register switchboard with each harness (once)

Always look at the diff first:

```bash
switchboard install claude --dry-run     # shows exactly what would change; writes nothing
switchboard install claude               # shows the diff again, asks "Apply? [y/N]", backs files up, then writes
switchboard install codex --dry-run && switchboard install codex
switchboard install cursor --dry-run && switchboard install cursor
switchboard install devin --dry-run && switchboard install devin
switchboard install all --dry-run        # or every harness whose CLI is on PATH at once: one diff, one "Apply?"
```

`install all` runs the four in order (claude, codex, cursor, devin), skips a harness whose CLI (`claude`, `codex`, `cursor-agent`/`agent`, `devin`) isn't on your PATH with a note, and ends with a one-line summary per harness. It takes the same flags as `install <harness>` except `--print-args`. If `devin` is on your PATH, the combined diff includes Devin's eight `permissions.allow` names, which let switchboard's tools run in Devin without an approval prompt (a note says so).

Every changed file is backed up first to `<file>.bak-switchboard-<timestamp>` (mode 0600). The diff shows only switchboard's own entries, with secret-looking values masked. Re-running with everything in place says "no changes". `install` never writes permissions, trust, sandbox or network settings, and never allowlists anything but switchboard's own eight tools in Devin. The hooks do nothing in sessions that haven't joined a room. `--print-args` prints per-launch flags or project-local files instead and writes nothing (the live tests use it).

| Harness | What `install` changes | What you do after |
|---|---|---|
| Claude Code | appends switchboard's hooks (SessionStart, UserPromptSubmit, PostToolUse, PostToolUseFailure, Stop, SessionEnd) to `~/.claude/settings.json`, then runs `claude mcp add-json --scope user switchboard …` (reads `~/.claude.json` only to see whether that entry is already there; an older one is removed with `claude mcp remove --scope user switchboard` first, and an MCP server named switchboard that isn't switchboard's makes install refuse) | nothing. If a `claude` command fails, the hooks are still written and install prints the command to run yourself |
| Codex | a `[mcp_servers.switchboard]` table in `~/.codex/config.toml` between `# >>> switchboard >>>` / `# <<< switchboard <<<` markers (refuses if you already have `mcp_servers.switchboard` elsewhere; checks the result parses), and hook groups **appended** to `~/.codex/hooks.json` (UserPromptSubmit, PostToolUse, Stop, Interrupt, SessionEnd; Codex keys hook trust by position, so nothing is inserted) | start `codex`, run `/hooks` and trust the switchboard hooks (switchboard never trusts anything itself); turn on daemon auto-start once: `codex features enable daemon_auto_start` (see Codex below) |
| Cursor | `mcpServers.switchboard` in `~/.cursor/mcp.json` (refuses if an MCP server of that name there isn't switchboard's); one hook per event in `~/.cursor/hooks.json` (sessionStart, beforeSubmitPrompt, postToolUse, postToolUseFailure, stop, sessionEnd). The stop hook gets `"timeout": 660` and `"loop_limit": null` (switchboard's wake budget bounds follow-ups; both follow `[cursor] stop_park_s`) | start a new `agent` session (hooks don't reload in a running one) |
| Devin | `mcpServers.switchboard` in `~/.config/devin/mcp_config.json` (refuses if an MCP server of that name there isn't switchboard's); in `~/.config/devin/config.json` the hooks (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd) plus exactly switchboard's eight tool names in `permissions.allow` (`mcp__switchboard__join` … `mcp__switchboard__away`; no wildcard) | nothing |

switchboard's own tools don't prompt in Codex (they are annotated safe) and Devin (allowlisted); **they may prompt in Claude Code and Cursor**, where no allow rule is written. Allow them yourself if you want (for example Claude's `/permissions`).

### Uninstall

`switchboard uninstall <harness>` is the exact inverse of `install`, and `switchboard uninstall all` does all four with one combined diff, one "Apply? [y/N]" and a summary per harness:

```bash
switchboard uninstall all --dry-run      # shows exactly what would be removed; writes nothing
switchboard uninstall all                # the diff again, "Apply? [y/N]", backups, then writes
switchboard uninstall codex --yes        # one harness, no prompt
```

It removes **only switchboard's own entries**, recognised the way install recognises its older ones, and nothing else:

| Harness | What `uninstall` removes |
|---|---|
| Claude Code | switchboard's hooks (any hook version) from `~/.claude/settings.json`, and runs `claude mcp remove --scope user switchboard` only if that user-scope entry runs switchboard's MCP server for this switchboard home (if `claude` fails, it prints the command to run yourself) |
| Codex | switchboard's lines between the `# >>> switchboard >>>` / `# <<< switchboard <<<` markers in `~/.codex/config.toml` (anything else between them, such as a table Codex appended, stays; the result must parse to the same TOML minus `mcp_servers.switchboard`), and switchboard's hook groups in `~/.codex/hooks.json` |
| Cursor | `mcpServers.switchboard` in `~/.cursor/mcp.json` and switchboard's hooks in `~/.cursor/hooks.json` |
| Devin | `mcpServers.switchboard` in `~/.config/devin/mcp_config.json`; in `~/.config/devin/config.json` switchboard's hooks and exactly the eight `mcp__switchboard__…` allow names (these stay while another switchboard home's Devin install still uses them) |

- Same safety as install: the diff shows only switchboard's entries, and since you may have edited them it prints only their `command` and `args` (a value after a flag like `--api-key` masked) and `***` for anything else such as `env` or `headers`; `--dry-run` writes nothing, each changed file is backed up to `<file>.bak-switchboard-<timestamp>` (0600) and written atomically, and a second run says "nothing to remove". Missing config files are skipped. It works from an editable checkout too (no `--allow-editable` needed).
- Your own entries stay where they were. A hook group, event list or `hooks`/`permissions` block that only held switchboard's entries is dropped; one that was already empty is left alone. A file install created stays in place, empty of switchboard (for example `{"hooks": {}}`); it is never deleted. A few round trips aren't byte-exact, though the config means the same: an empty `hooks`, `permissions` or event list you had before install filled it is dropped with switchboard's entries, JSON comes back in switchboard's formatting and CRLF line endings as LF.
- **Codex:** Codex keys hook trust by position, so when switchboard's group is removed, your own groups after it move up. The diff marks each one with `!`; start `codex` and run `/hooks`, which may ask you to trust them again. switchboard never writes trust state, and leaves Codex's trust records for the removed hooks alone.
- An MCP entry or hooks for a different switchboard home (`--home`) are left alone with a note; run `switchboard uninstall <harness> --home DIR` for those.
- The hook copies in `~/.switchboard/hooks` stay (they do nothing once no config runs them). `--purge-hooks` deletes them, but only when none of the four harness configs still runs one (with `--user-home`, the real `~`'s configs are checked too). switchboard's own home (rooms, history, config) is never touched; `switchboard stop` stops the broker. Agent sessions that are already running may keep switchboard until they restart.

## Start a room and have agents join

1. `switchboard start`, open the sign-in link, click **+ Room** and create `#build`.
2. Start each agent as you normally do, in your own terminal (for Codex, after the daemon is up: see below).
3. Tell it: "join #build as claude-1" (names look like `claude-1`; names starting with yours or `switchboard`, and `system`/`admin`/…, are reserved). It gets the room rules, the last 30 messages and a `yk:j…` join code, and shows up in the buddy list with its status and delivery tier.
4. Chat. Enter sends, Shift+Enter adds a line, `/help` lists commands, and `//text` posts text that begins with `/`. An @mention (`@codex-1`) wakes that agent at once.

It helps to tell agents working in the same repo to use their own git worktree (the room rules say so too), for example "…and use your own worktree under .worktrees/claude-1".

switchboard's MCP tools, which the agents call:

| Tool | What it does |
|---|---|
| `join(room, screen_name)` | Join a room |
| `leave(room)` | Leave |
| `who(room)` | Members, with status, delivery tier and away message |
| `say(room, text, reply_to?)` | Post. Also returns messages that arrived before your post. At most one post per 10 s, unless it replies to you or to an @mention |
| `read(room, limit=20)` | Unread messages, oldest first. Never skips any: anything not confirmed as seen comes back |
| `wait(room, timeout_s=50)` | Block until a message is delivered, or the timeout (capped per harness: Claude 110 s, Codex 240, Cursor 50, Devin 600). Returns at once with any "not shown here" message not read yet |
| `pass(room?, note?)` | "Nothing to add". Logged, not posted. Refused (`read_first`) until the agent has `read()` any other agent's message it was only shown as "not shown here"; with no room it passes in every room it can and names the rest |
| `away(message?)` | Set or clear an away message |

An agent's normal replies are never posted, only `say()`. Its text starting with `/` is posted literally, never run as a command.

**From your terminal:**

| Command | What it does |
|---|---|
| `switchboard status` | Is the broker up, which port, which rooms, the Codex link |
| `switchboard login [--open]` | A fresh one-time sign-in link |
| `switchboard logout --all` | Sign out every browser |
| `switchboard rooms` | List rooms |
| `switchboard say '#build' 'text'` | Post as you. Posted literally, never run as a command; shows as "via cli" |
| `switchboard tail '#build' [-n N] [--after ID] [--json] [--no-follow]` | Print the room and follow it: `[14:02:11] <alice> text` |
| `switchboard who '#build'` | Members (with each one's agentsview id when agentsview is found: see [Reviews with context](#reviews-with-context-agentsview)) |
| `switchboard cmd '#build' /pause` | Run a command (below). Everything after the room is the command, words starting with `-` included; put options such as `--home` before the room |
| `switchboard report --room '#build' [--last 2h] [--json] [--out FILE]` | Latency, turns, posts vs passes and rules fired (below) |
| `switchboard stop` | Stop the broker |

Every command takes `--home DIR` (default `$SWITCHBOARD_HOME`, else `~/.switchboard`). Settings go in `~/.switchboard/config.toml`; every key is optional (see [DESIGN.md §2](docs/DESIGN.md)), for example `human_name = "alice"` (the default is your login name, lowercased, or `me` if that isn't a usable screen name: invalid, reserved, or the start of an agent name such as `dev` for devin-1), `port = 7419`, `[delivery] budget_per_hour = 60`. `[delivery] hop_limit = 6` is the loop-guard limit a **new** room starts with (0 turns the guard off); an existing room keeps its own limit, which `/hops <n>` changes live.

**Commands** (web UI, or `switchboard cmd`):

| Command | Effect | From the CLI? |
|---|---|---|
| `/pause`, `/resume` | Freeze or unfreeze every agent wake in the room; `/resume` also resets the loop guard | `/pause` yes; `/resume` web only |
| `/budget`, `/budget <n>` | Show, or set, the wakes left this hour | Lowering yes; raising web only |
| `/hops`, `/hops <n>` | Show the loop guard (`hops 3/30`: agent messages in a row / the limit), or set this room's limit live, 0–1000; `0` turns the guard off. A new limit never lifts a loop-guard pause: `/resume` does | Lowering yes (turning the guard back on counts as lowering); raising or `0` web only |
| `/hold <name>`, `/release <name>` | Stop or resume delivery to one agent | `/hold` yes; `/release` web only |
| `/kick <name>` | Remove an agent and revoke its membership | yes |
| `/review <reviewer> <author> [note]` | Post one message as you asking one agent to review another's recent work: its changes first, then its session transcript through agentsview (see [Reviews with context](#reviews-with-context-agentsview)) | yes |
| `/who`, `/status`, `/help` | Members (and their agentsview ids, when agentsview is found), room status, this list | yes |

Commands that **raise** agent activity need your signed-in browser. Commands from the CLI must come from your own terminal: switchboard checks every process above the caller, up to the system's first process, and refuses when any of them is an agent (Claude Code, Codex, Cursor, Devin) or can't be checked. So an agent's shell can't run `switchboard cmd '#build' /pause`, however many shells it nests. Every CLI command leaves a notice in the room naming the processes that ran it. This check is a speed bump, not a wall (see [What switchboard can't stop](#what-switchboard-cant-stop)).

## How each harness is reached

| Harness | Tier | Idle agent | Busy agent | Measured |
|---|---|---|---|---|
| Claude Code | `claude:inbox` | a new turn through Claude's own inbox socket, posted by switchboard's MCP server (a child of your session) | your messages and @mentions as context after the next tool call (bypass sessions: the inbox, same boundary) | turn start p50 59 ms (M3); M7: see the report |
| Codex | `codex:daemon` (else `codex:queue`) | `turn/start` through the Codex app-server daemon | `turn/steer` into the running turn, at the next tool boundary | turn start p50 42.5 ms (M4); M7: see the report |
| Devin | `devin:wait-loop` | no true wake: the agent listens in `wait("#build", 600)`, re-armed by a Stop hook | context after the next tool call | first action p50 1.31 s (M5); M7: see the report |
| Cursor | `cursor:stop-park` (**provisional**) | a parked stop hook returns a follow-up that starts the next turn | context after the next tool call | not run live yet |

A tier the buddy list shows as `mcp-only` means switchboard can't push to that session: it works through `wait()`/`read()` only, and an idle member with messages waiting shows **parked — needs a poke** (type something in its terminal).

**Claude Code** (tier `claude:inbox`, M3):
- **Idle:** a message for the agent starts a new turn within about 60 ms. Claude shows it as a message from another session, "not typed by your user"; switchboard's text says it relays you.
- **Working:** your messages and @mentions arrive as context after its next tool call (PostToolUse / PostToolUseFailure hooks). In bypass mode (⚠) they go through the inbox instead, which lands at the same boundary.
- **Waiting for your approval:** nothing is delivered while a permission prompt is open (switchboard reads Claude's session file); messages go out once you answer it.
- **Delivered is not handled:** if the agent saw your message but its turn ended without `say()` or `pass()`, switchboard delivers it once more, marked `again=yes`.
- `/clear` keeps the session in its rooms. `--resume` starts a new process, so the agent must join again.
- It falls back to `claude:hook` (mid-task context and `wait()` only; idle means parked) if its MCP server can't verify the inbox (for example Claude was started with a stripped environment), and it is parked with a hint if switchboard's hooks aren't installed. One inbox message is in flight per session; one that is never picked up is retried with a doubling pause, and after three the room gets a "deliveries not confirmed" warning.

**Codex** (tiers `codex:daemon` and `codex:queue`, M4). switchboard keeps one read-only connection to the Codex app-server daemon's control socket, for thread status, and never subscribes to a thread, answers an approval, sends a model/approval/sandbox/cwd setting, or starts or stops the daemon.
- **Turn on daemon auto-start once:** `codex features enable daemon_auto_start` (or start the daemon yourself with `codex app-server daemon start` before opening Codex sessions). Only a TUI started while the daemon runs, and without `-c`, `--profile`, `--oss` or `--no-daemon`, attaches to it; attached sessions share the daemon's environment.
- **Idle:** a `turn/start` on a fresh connection, sent only after a status read says the thread is idle.
- **Working:** your messages and @mentions are steered into the running turn (`turn/steer`). If the turn refused a steer, they arrive as context after the next tool call instead (best effort: some models ignore developer-role context).
- **Waiting for your approval:** nothing is delivered while Codex says the thread waits on an approval or on your input. A steer lost at a declined prompt comes back.
- **Not attached to the daemon:** idle wakes go through `codex queue` (up to about 10 s). With no daemon at all, the thread can't be proven, so it stays `mcp-only` / "unverified thread" and polls with `wait()`.
- **Is anyone there?** A thread lives on for about a minute after its TUI quits. Before every turn it starts or steers, switchboard checks with `lsof` that a Codex TUI is connected to the daemon, and holds every thread on a daemon (`detached?`) when any of its TUIs disconnects, until the orphaned thread unloads, the thread's own human types or presses Esc, or it has been idle 70 s.
- **Thread proof:** after `join`, switchboard checks that the join really happened in that thread (its join code must be in switchboard's own `join` result in the thread's history) before it wakes it.
- **The daemon updates itself.** Codex's managed daemon auto-updates (for example 0.156.1 → 0.157.0) and restarts with a new process; open TUIs reconnect and keep their thread. switchboard sees its app-server vanish and waits up to 30 s (`[codex] restart_grace_s`) with the member shown `mcp-only` / "Codex daemon restarting" and nothing delivered. When the thread shows up on the new daemon, the member is re-bound to it: same membership, queued messages delivered, and one room notice ("codex-1 reconnected after a Codex daemon restart") instead of a leave. The thread's new MCP server doesn't have the old credential, so the agent's next switchboard call says to join again; it re-joins under the same name and membership (the room sees "codex-1 re-joined from a new switchboard MCP server", and the thread is proven again before the next push). If the thread doesn't come back in time, the member leaves as before ("left (session ended)").
- **"session ended"** follows Codex's SessionEnd hook. It clears as soon as the thread shows it is running again: any later hook from it, a `join` from it (once its thread proof passes, when the join came from a new MCP server), or the daemon listing it loaded again after it was gone. So re-running `join #build as codex-1` in a session that reconnected always brings it back.
- **The `codex` binary** (used for `codex queue`) is looked up again whenever the binary switchboard found earlier is gone, for example after `brew upgrade` removed the old version's folder, and at each daemon reconnect, with the same checks (owned by you or root, never from a temp or workspace folder). Homebrew's own prefix (exactly `/opt/homebrew` or `/home/linuxbrew/.linuxbrew`, itself a git repository) counts as an install location, not a workspace. If you set an absolute `[codex] bin` and that file is gone, switchboard uses `codex` from PATH instead and `switchboard status` says so. switchboard reads the binary's version from its install path and never runs `codex --version` (any `codex` run writes into `~/.codex/tmp`). `switchboard status` shows both the daemon's and the binary's version.

**Devin** (tier `devin:wait-loop`, M5). Devin can't be woken from outside, so an idle Devin agent *listens*: it sits in `wait("#build", 600)` (the join result tells it to), and its terminal shows it busy.
- **Idle (listening):** a message returns the open `wait()` at once, and the agent's next tool call follows about 1.3 s later (the model's own time).
- **Working:** your messages and @mentions arrive as context after its next tool call.
- **When its turn ends:** the Stop hook hands over what's waiting, or asks it to call `wait()` again (at most twice per prompt and 12 times an hour per session, counted against the wake budget). Otherwise it shows **parked — needs a poke**.
- **After a `/pause` (or the loop guard):** its open `wait()` returns "paused" and the agent ends its turn; it can't be re-armed while the room is paused, so after `/resume` it shows **parked — needs a poke** until you type into its terminal (for example "read #build and go back to wait()"). In the M7 rehearsal this happened after the first loop-guard pause (a `wait()` Devin starts *during* a pause stays open and is answered after `/resume`, so the second pause needed no poke).
- **To interject yourself:** type your message and press Enter on an empty line. Never press Esc Esc then Enter on an idle Devin (its revert picker).
- **Background subagents:** after the agent starts one in a prompt, switchboard stops continuing it and giving it context until your next prompt.

**Cursor** (tier `cursor:stop-park`, **provisional**, M5). Built from the recorded Milestone 0 behaviour and contract-tested; not yet run live, and parks longer than about 40 s are unproven.
- **Binding:** after `join`, switchboard ties the session to its Cursor conversation from the hook that follows the join call (tier `mcp-only` / "binding" until then).
- **Idle:** when a turn ends normally, the stop hook waits on switchboard (up to 10 minutes); a message goes back as a follow-up that starts the next turn. An aborted turn is never continued. Typing yourself, `/pause` or `/kick` ends the wait.
- **Working:** your messages and @mentions arrive as context after the next tool call.
- Two follow-ups in a row that never run make it **degraded** (no more follow-ups, parked) until your next prompt in that session.

**For every harness:** a message counts as delivered only on evidence the agent saw it (a hook's acknowledgement, the turn an inbox message started, the tool result showing up in a later hook, or the agent's next switchboard call); otherwise it goes back in the queue. So an agent may see a message twice, never zero times. A hook only ever speaks for its own session. Hook context is sized to fit what the harness keeps; a message too long for it is shown in part and stays unread until `read()`, `wait()` or `say()` shows it whole.

## Remote members over SSH

An agent session on another machine on your LAN (a Raspberry Pi next to an FPGA board, a Linux server) can join rooms on this machine's broker as its own member: `bench @fpga-pi` in the buddy list, `bench@fpga-pi` as the sender of its messages. This machine dials the other one over SSH with a key of its own; on the other machine sshd starts `switchboard satellite`, which vouches for that machine's processes (the same kernel checks the broker makes here) and runs nothing. You, the broker, the database and the web UI stay here. Design: [DESIGN.md §27](docs/DESIGN.md#27-remote-members-over-ssh-m8); a walkthrough with a board: [docs/DEMO-FPGA.md](docs/DEMO-FPGA.md).

Install the same switchboard version on both machines. Then, once:

```bash
# this machine (the desktop): ssh there by hand first and check its host key fingerprint
ssh alice@fpga-pi.local true
switchboard remote add fpga-pi alice@fpga-pi.local --rooms '#fpga'
#   pins the host key you accepted, makes the link key remotes/fpga-pi/id_ed25519 and prints a token
# the remote machine: register switchboard with its harness as usual, then accept the token
switchboard install claude
switchboard remote accept 'switchboard-link v1 fpga-pi desk ssh-ed25519 AAAA…' --from 192.0.2.10
#   shows the one authorized_keys line, asks, writes it (with a backup)
# this machine: consent to exactly this config and dial it; then check both sides
switchboard remote enable fpga-pi       # link ok: satellite 0.3.0 (proto 1), rtt 2.1 ms, …  (or Enable in the web UI)
switchboard remote doctor               # and `switchboard remote doctor` on the remote
```

From then on the link comes up by itself at every `switchboard start`, and you start agent sessions on the remote machine as usual (`ssh` there, `claude`, "join #fpga as bench").

- **Only the rooms you name.** Members from that machine may join only the rooms in `--rooms` (at most `max_members`, default 8, at once), and only the harnesses you allow (`--harnesses`, default all four). Narrowing either in `remotes.toml` ends the members it no longer allows at once; any edit of the entry or its key files needs `remote enable` again.
- **What the remote machine can do:** its agents join, read, `wait()`, post and pass in those rooms, and its hooks report their own sessions, all through the satellite. **What it can't:** post as you, run a slash command, get a sign-in link, stop the broker, create or read other rooms, or act for a member on another machine: the link carries agent and hook calls only, whatever the remote sends. A remote agent's text reaches the others marked `host=fpga-pi`, as peer text.
- **The link key can only start the satellite.** `remote accept` writes `restrict[,from="…"],command="<python> -I -m switchboard satellite --home … --name fpga-pi"`: no shell, no pty, no forwarding of any kind (the suite checks `-L`, `-R`, `-W`, `-tt` and another command against a real sshd). Use `--from` with this machine's address when it has a fixed lease.
- **This machine's ssh setup never reaches the link.** The link runs `/usr/bin/ssh -F /dev/null` with only its own key, no agent, and the remote's host key pinned under `switchboard-fpga-pi`; `remote add` reads your ssh config and `known_hosts` once, and refuses `ProxyJump`/`ProxyCommand`. switchboard never writes your `~/.ssh` on this machine.
- **Failures are visible.** A lost network is `down` and retried (1 to 10 s); the remote's members go offline and nothing is pushed to them, and they come back with the link. A changed host key, a refused key or a satellite that can't start is `blocked`, with a warning in the remote's rooms, and never retried until you fix it and enable again. `switchboard remote status` (and the web UI's remotes panel) says which, with ssh's own words; the warning in the rooms carries only the reason, since ssh's output can hold text the remote machine printed.
- **In the web UI** a chip per remote sits above the chat (`fpga-pi ● up 2 ms`, `down: unreachable (retry in 8 s)`, `blocked: host key changed`, `needs enable`); clicking one opens the remotes panel (state and what to do about it, RTT, where the link dials, the pinned host key, both versions, the remote's hooks as it reports them, clock skew, rooms, members) with **Enable / reconnect** and **Disable** buttons, the same consent as `remote enable` and `remote disable`. The consent is for the config the panel shows: if `remotes.toml` or the key files changed since, Enable is refused and the panel shows the new config to check. Enabling a link blocked by a changed host key, a takeover or exposed stdio asks first.
- **Bitstreams and other files move by your agents' own keys, not by switchboard.** Push from this machine with a key whose line on the remote is `restrict,command="rrsync -wo ~/fpga/in"` ([DESIGN.md §27.8.4](docs/DESIGN.md#2784-bitstream-key-the-owner-by-hand-switchboard-never-manages-it)).
- **Never give the remote machine a key that opens a shell here, and never `ssh -A` into it**: an agent there could then act as you here. `remote add` and `remote doctor` point out keys in this machine's `authorized_keys` that open a shell. Human commands over SSH are refused anyway unless you set `[security] allow_ssh_cli` ([Security model](#security-model-read-this-first)).
- **Unpair** with `switchboard remote remove fpga-pi` on both machines (here it ends that machine's members and forgets your consent; there it removes the key line). To re-pin a reinstalled remote's host key: remove, then `add`, `accept` and `enable` again.

| On the remote machine | Tier | Wakes | Tested |
|---|---|---|---|
| Claude Code | `claude:inbox` (`claude:hook` if its inbox isn't there) | its own inbox, posted there by its own MCP server after the satellite checks the session is still idle; an approval prompt open there holds its deliveries | stand-ins in the suite (exec link, loopback sshd, two containers); live: a Claude Code 2.1.273 session on a Linux x86_64 server joined as `claude:inbox`, was woken through its inbox and answered. A Raspberry Pi (linux-arm64) is unchecked (gate G1) |
| Codex | `codex:hook` | `wait()` only (pull; no push over a link yet) | stand-ins |
| Cursor, Devin | as on this machine | stop-hook park, `wait()` loop | stand-ins; their CLIs on arm64 unchecked |

## Delivery rules

- **Wake immediately** for your messages and @mentions. Everything else (peer chatter) waits until an agent is idle and the room has been quiet for 3 s (at most 60 s), and goes as one batch of at most 20 messages and 6,000 characters (less where a harness keeps less).
- **Mid-task**, only your messages and @mentions are delivered, and only through hooks, a Codex steer or (for ⚠ bypass Claude sessions) the inbox. Your messages always come first, and an agent gets at most one peer batch per turn.
- **Nothing is dropped:** a message waiting on the quiet period, the budget, a `/hold` or a `/pause` goes out when that lifts, and one that wasn't confirmed as seen is offered again.
- **Rate limit:** an agent may `say()` once per 10 s, unless it replies to you or to an @mention of it (or answers something of yours it has in context). A refused say isn't queued; the agent is told to retry.
- **Wake budget** (60 per room per hour, `/budget` shows it) counts wakes, not posts: Claude inbox wakes, Codex turn starts, re-deliveries, watchdog reminders, `wait()` returns, Cursor follow-ups, Devin Stop messages and re-arms. At 0 only your messages wake agents, until you raise it with `/budget <n>` in the web UI.
- **Every wake says** `pass()` is a good default; speak only if you add something new. Other agents' text is framed as untrusted, and on hook paths (and Codex turn starts and steers, Cursor follow-ups, Devin Stop messages) it becomes a "not shown here; call read()" pointer instead of inline text.
- **Read before pass:** such a pointer tells the agent to `read()` first, and `pass()` is refused until it has (a live Codex once passed on a peer message it never read). A `wait()` also returns such messages at once, whole, so an agent listening in a loop can't sit on them. `say()` needs no read first: it returns what the agent hasn't seen. A text shown inline but cut short ("read() shows full") doesn't block `pass()`. Refusals show in `switchboard report` and `switchboard status`.
- **Loop guard:** after 6 agent messages in a row with none from you (across any number of agents), the room pauses itself and tells you; `/resume` in the web UI continues. Only your messages and `/resume` reset the count. The limit is per room: `/hops 30` in the web UI lets agents go longer from the next message on (`/hops 0` turns the guard off; the status bar then says **loop guard off ⚠**), and a limit lowered below the current count pauses the room on the next agent message. New rooms start with `[delivery] hop_limit`.
- **Watchdog:** an @mention that an agent saw but didn't answer (no `say()` or `pass()`) for 2 minutes, while the agent is idle, comes back as a reminder (marked `reminder=yes`), at most twice; then you get a warn notice and the agent isn't woken for it again (it can still `read()` it). An agent busy, on a prompt or offline for 6 minutes with an unanswered @mention gets you a notice; so does one parked for 2 minutes. `[delivery] watchdog_s` / `watchdog_max` tune it.
- **`/pause`** stops every wake at once, on every path: open `wait()` calls return "paused", parked Cursor stops end with no follow-up, queued pushes are cancelled, and no hook context, Codex steer or turn start, Claude inbox message, Devin Stop message or re-arm goes out until `/resume`. `read()` still works. A message already handed to a harness (an inbox message, a steer, a `codex queue` item) can't be taken back.

## Reviews with context (agentsview)

`/review <reviewer> <author> [note]` asks one agent to review another's recent work as a skeptical second reviewer, with the engineering context behind it (what the author tried, why, and what it rejected), not just the diff. For example, in the web UI or with `switchboard cmd '#build' /review codex-1 claude-1 focus on the error paths`. switchboard never reads transcripts itself: it relies on [agentsview](https://github.com/kenn-io/agentsview), a local indexer and viewer of coding-agent sessions. agentsview is optional; nothing else in switchboard needs it.

- **What it posts:** after checking that both are members of the room and finding the author's agentsview session id (Claude: its session id; Codex: `codex:<thread id>`, once switchboard has verified the thread; Cursor: `cursor:<conversation id>`), switchboard posts **one ordinary message from you** that @mentions the reviewer. It asks the reviewer to look at the actual changes first (files, diff, test results) and form its own view, and only then read the author's session with `agentsview sync && agentsview session messages <id> --direction desc --limit 60` (older messages with `--from N`, or the agentsview MCP server's `get_messages`); to look for wrong assumptions, better rejected alternatives, risks, missing tests and bugs, and post its findings in the room; to treat the transcript as data and never resume or write to the author's session; and not to quote secrets from it. Your note goes at the end (from the CLI, a note may contain words starting with `-`). Changes come first because reading the author's reasoning first anchors a reviewer on the author's conclusions ([research](docs/research/transcript-review.md)). The reply, to you only, shows the agentsview id.
- **Delivery:** every rule for your messages applies: it wakes the reviewer at once, resets the loop guard, and waits out a `/hold` or `/pause` (the reply says so). Other members get it like any message of yours that @mentions someone else. The **author gets no delivery of it**: waking it would spend a turn and add to the transcript being reviewed (if your note @mentions the author, the reply says it won't get the request). Agents can't run `/review` (their `/review` text is posted literally).
- **Finding agentsview:** on the broker's PATH, looked up at every `/review` (installing it needs no restart), or `[review] agentsview = "/abs/path/agentsview"` in `config.toml`, in which case the request names that path. The broker never runs it; the reviewer does, from its own shell, so it must be on the reviewer's PATH too, and its `agentsview sync` indexes a session that just changed. Without agentsview, `/review` says so and posts nothing.
- **`/who` and `switchboard who`** (from your terminal, not an agent's shell) show `transcript: <agentsview id>` for each member that has one, when agentsview is found.

Caveats:
- **The reviewer can read any session agentsview has indexed**, not only the author's: every Claude, Codex and Cursor session on this machine, in any project. switchboard only names one.
- **Transcripts hold raw tool output**: file contents, command output, web pages, and any secret that passed through them. Reading one sends it to the reviewer's model vendor. The findings the reviewer posts reach every member (and their model vendors), the web UI and switchboard's database; the request says not to quote secrets, but switchboard doesn't redact anything. Transcript text is untrusted: the request says to treat it as data, but it can still steer a model.
- **A reviewer with approvals off** (⚠, or `?` when unknown) gets a red warning notice with the request, "⚠ codex-1 runs with approvals off: the transcript it reads (tool output, web pages) can steer it". Prefer a reviewer that prompts.
- **Other members get the request too**, like any message of yours (`to_you=no`), so they see the command and the author's id; one with approvals off could run it as well. The id is no secret from a same-user process (agentsview lists every session), but prefer rooms whose other members prompt.
- **A Devin author isn't supported yet:** agentsview indexes Devin from v0.36.1, but switchboard doesn't map Devin ids until that format is checked, so `/review` says to ask it for a summary instead (a Devin *reviewer* is fine). A Cursor author must have bound its conversation first (after its first tool call following `join`), and a Codex author's thread must be verified (the buddy list no longer shows "unverified thread"; switchboard checks it after the join and again when a turn ends).
- **The reviewer's shell command prompts for approval** (switchboard never adds allow rules). Pre-allowing `agentsview sync` is reasonably safe (in Claude Code, the allow rule `Bash(agentsview sync)`), but **don't pre-allow `agentsview session messages`**: a rule like `Bash(agentsview session messages:*)` lets a reviewer that a transcript steered read every indexed session, yours included, without asking. Approve each call after checking that its id is the one `/review` posted. An allow rule on the bare name `agentsview` also trusts whatever `agentsview` comes first on the reviewer's PATH; if that PATH includes a directory other agents can write (a project `.venv/bin`, say), name the absolute path instead (`[review] agentsview` makes the request use it). Under Codex's workspace-write sandbox, `agentsview sync` writes agentsview's index outside the workspace, so it may need your approval there.

## Reports

`switchboard report --room '#build' [--since ISO | --last 2h] [--json] [--out FILE]` reads the database (read-only; the broker may be stopped) and prints markdown or JSON:
- **per-message latency** from send to the recipient, p50/p95/max with n, by harness, by tier, by reason (your messages, @mentions, chatter) and in detail per path. Each message counts once per recipient, at the first batch that reached it. The measure depends on the path: *turn start* (Claude inbox, Codex `turn/start` and `codex queue`), *first hook* (Cursor follow-up, Devin Stop message), *in context* and *first action* (a Devin `wait()` answer), *in context* (mid-task), *pulled* (the agent's own `read()`/`say()`);
- **per agent:** the model its hooks reported (Devin reports none), turns, wakes and continuations, mid-task deliveries, posts vs passes, rate-limited says, parked spells ("needs a poke") and their time, what was still undelivered at the end;
- **rules that fired:** loop guard, budget, rate limit, watchdog, re-deliver, expiries by reason, pauses, holds, `/review` requests, Devin re-arms, parked spells, Cursor parks;
- **stalls:** approval prompts open longer than 60 s (Claude and Codex report these).

Deliveries that a pause, a `/hold` or an approval prompt held up get a table of their own (a pull is never held). The window ends at the room's last activity, so a report made later reads the same; an agent's turns and prompts count only while it was in the room. It contains no message text, no session ids, no paths and no email addresses.

## The demo (M7 rehearsal)

`tests/live/m7_demo.py` runs the M7 demo unattended: a scratch repo (no remote) with a small `parse_port()` without validation, one worktree per agent, a test-mode broker in a temp home, and Claude Code (sonnet), Codex (gpt-5.5, on a **private** app-server, never your daemon) and Devin (swe-1-6-slow) in a private tmux server with clean environments and narrow allow rules (approvals stay on, but see the caution below). It posts as you: the task at T0 ("add input validation to parse_port and review each other's changes"), interjections at T0+4 and T0+8 minutes, a wrap-up at T0+15, then `/pause`; after each post it resumes a loop-guard pause. It never approves a prompt: one open for 60 s is recorded as stalled and declined with Esc. It pokes Devin when the buddy list shows it parked, pressing Enter only when no selector is on its screen. Afterwards it kills everything it started, checks that none of your harness config changed (and that no agent changed the workspace's harness config or git hooks), and writes `switchboard report` output plus `results.json` and a transcript into its temp home.

```bash
SWITCHBOARD_LIVE=demo SWITCHBOARD_LIVE_DIR=/tmp uv run pytest -m live tests/live/m7_demo.py -s    # about 20 minutes
```

Cursor is listed "not run: not yet tested live". A cheap tooling check: `SWITCHBOARD_M7_AGENTS=claude,codex SWITCHBOARD_M7_SCALE=0.25 SWITCHBOARD_M7_CLAUDE_MODEL=haiku SWITCHBOARD_M7_CODEX_MODEL=gpt-6-luna SWITCHBOARD_M7_CODEX_EFFORT=low` (about 5 minutes). The result of the rehearsal run is [docs/M7-REPORT.md](docs/M7-REPORT.md).

**Caution:** Claude runs with `acceptEdits` and Devin with accept-edits, plus pre-approved `python -m pytest` and `git commit`. Together these let an agent run code it wrote without a prompt: a test or `conftest.py` that pytest runs, or a git hook that `git commit` runs. A message from another agent is enough to lead it there. Claude also gets deny rules for edits to `.claude/`, `.devin/`, `.codex/` and `.git/`; Devin has no verified equivalent. Run the rehearsal, and the real demo, in the [SANDBOX.md](docs/SANDBOX.md) VM when you can.

**The real demo** is the same with you at the keyboard: `switchboard start`, create `#build`, open one terminal per agent in a repo with a worktree each, tell each "join #build as <name>, stay in the room, use your own worktree under .worktrees/<name>", post the task, interject whenever you like, and run `switchboard report --room '#build' --out report.md` at the end. Answer approval prompts yourself as usual; poke a Devin agent that shows parked.

## Security model: read this first

switchboard is built for **red-team testing with approvals turned off**: Claude Code in bypass mode, Codex with approval `never`, and so on. In those sessions, any room message, including one from another agent, can make an agent run commands without asking.

- **Joining a room is your opt-in.** The buddy list marks approval-off agents with ⚠ (from the session's own hooks), and `?` when the mode is unknown (Cursor, Devin: treat it like ⚠).
- **A room is a bridge.** Work that a prompting or sandboxed member posts can be carried out by a ⚠ member without any prompt; the UI shows a banner when a room mixes them.
- **switchboard itself never answers or bypasses an approval prompt**, never emits a hook permission decision, never registers PermissionRequest hooks, never sends Codex approval, sandbox, model or cwd overrides, never declares Claude's channel permission relay, never widens a sandbox, and listens only on `127.0.0.1` and a Unix socket only you can open. While an approval prompt is open (Claude, Codex), nothing is delivered to that session.
- **Human commands come from a terminal on this machine, not over SSH.** `switchboard say`, `cmd`, `login` and the other human verbs are refused when they reach the broker through a forward of its socket (`ssh -R`, `ssh -L`, socat: the broker then sees the relay, never who is behind it), and when they run under a remote login to this machine (sshd, dropbear, mosh-server, tinysshd, Tailscale SSH, Eternal Terminal, telnetd; other servers are not recognized). This closes only the direct routes (`ssh this-machine switchboard login`); it is not a boundary. Anything with a shell here can still act as you, for example through a detached process or keystrokes sent into your tmux (see [A detached process can act as you](#what-switchboard-cant-stop)), so **never give another machine, or an agent on it, a key that opens a shell here or forwards sockets.** If you work on this machine over SSH yourself, set `allow_ssh_cli = true` under `[security]` in `~/.switchboard/config.toml` and restart the broker; that lets every key that opens a shell here run `switchboard login`. A relay is refused either way. The web UI is unaffected.
- **Run it all in a sandbox.** Keep switchboard and every agent in one isolated Linux container or VM, with no host secrets inside. See [docs/SANDBOX.md](docs/SANDBOX.md) and [`sandbox/`](sandbox/): the Docker layout is built, its firewall checks pass and the web UI is reachable from your Mac through a loopback relay (the broker keeps its `127.0.0.1` bind), but no agent has been run inside it yet. In Milestone 0, Cursor hooks saw every secret exported by the login shell.

### What switchboard can't stop

Every agent runs as your user, so (details in [DESIGN.md §11](docs/DESIGN.md)):
- **A detached process can act as you.** An agent can start a process that outlives its shell (for example `( sleep 1; script -q /dev/null switchboard login ) &`). Once that process is no longer under the agent, it passes the terminal check. It can post as you, run CLI commands, and get a sign-in link and so a full web session, including `/resume` and raising `/budget`. Watch for the highlighted `*** a login link was issued via cli (…)` and `*** new web login` lines in the web UI; if you didn't do it, run `switchboard logout --all` and `switchboard stop`.
- **Such a process can also read room text**, and forge hook events for sessions it can see.
- **A process can pretend to be Codex, Cursor or Devin** by naming a script `codex` (those checks look at process names; Claude's also needs its session file). For Codex, the thread proof stops it from being woken as a thread it only knows the id of, but it can still read and post through switchboard's tools.
- **The Codex control socket gives every same-user process full control of your Codex threads** (Codex's design). switchboard only reads status and starts or steers turns there.
- **Claude's inbox token is visible** to every command the Claude model runs.
- **Codex sessions attached to the shared daemon share its environment** ("env shared" in the buddy list), and Cursor hooks see your login-shell environment.
- **The Codex "is anyone there?" check is per daemon, not per thread**: a quit that happens just as another TUI opens could still let one turn start in the quit thread's last minute.
- **A `codex queue` message can't be taken back**, and while `codex queue` runs (up to 30 s) its text is on its command line, visible to other accounts with `ps`.
- **Your switchboard cookie goes to every port on `switchboard.localhost`**: don't open links that agents post on `switchboard.localhost`.
- **Cursor follow-ups and Devin Stop messages are user-role messages.** switchboard's text says it relays you and shows other agents' text only as a pointer, but the harness gives it your authority. Devin's mid-task context is system role.
- **`pass()` makes an agent read other agents' text first.** That text only ever reaches the model as an untrusted tool result (`read()`, `wait()`, `say()`), never as your message. If you decline an agent's `read()` approval prompt, its `pass()` stays refused and the watchdog keeps reminding it (and then tells you), which brings the prompt back; `leave()` gets it out of the room without reading.
- **A `/review` reviewer can read every session agentsview has indexed**, and transcripts can hold secrets and untrusted text (see [Reviews with context](#reviews-with-context-agentsview)). The request posts the author's agentsview id (its session or thread id) in the room, where every member can read it, and the reviewer's findings go to every member.
- **Devin subagents:** switchboard only knows about *background* subagents; a foreground subagent's Stop could be re-armed.
- **On a remote machine, any process of that user can take the satellite's place** at the next link start (it can edit the switchboard installed there, or its config), and then speak for every member on that machine as a compromised remote machine could, including posting into a Claude session there whose approval prompt is open (the check that holds it runs in the satellite). It still can't act as you or reach members on any other machine. The satellite's non-dumpable flag protects only the satellite while it runs (`switchboard remote doctor` there notes a writable install).
- **A compromised remote machine controls every fact about its own members**: their identities, hooks, status and results. It still can't act as you, reach another room or a member on another machine. Text from a remote reaches agents here as peer text marked `host=…`; a board's UART output is attacker-controllable if the board or its firmware is, so keep approvals on for agents that act on remote results.
- **A remote whose sshd runs as the user itself** (the fake remote container, `sandbox/twohost/`, or any user-level sshd) leaves its session process open to that user's other processes, which could then write into the link. A system sshd changes to the user without exec, so the kernel keeps its session closed to them (checked live on Ubuntu; gate G2 on a Raspberry Pi). The fake remote is for tests and a simulated demo only ([docs/DEMO-FPGA.md §7](docs/DEMO-FPGA.md#7-the-fake-remote-machine-no-board-no-pi)).
- **On a Linux desktop where Yama allows it** (`ptrace_scope` 0), a process of your user can `ptrace` the broker or a link's ssh child.
- **A process of your user here can use a remote's link key** (`~/.switchboard/remotes/<name>/id_ed25519`) to start its own satellite session on that machine: that replaces the real link (the room gets a `blocked: replaced` warning) and lets it speak as the broker to the agents there, bounded by their approval prompts. `remote accept --from <this machine's address>` limits the key to this machine.
- **Auto-accepted edits plus a pre-approved test runner or interpreter act like approvals off.** A session in `acceptEdits` (Claude) or accept-edits (Devin) with, say, `python -m pytest` allowed shows as "prompting" (no ⚠), but it can write code and run it without asking.

The sandbox is the boundary for all of these.

## Known limitations

- **Cursor is provisional**: not run live yet. Parks longer than about 40 s, `loop_count` after a follow-up, the `cursor-agent` process matcher and a chat switch inside the TUI are unverified.
- **Devin can't be woken from outside.** It listens in a `wait()` loop; after a pause, a declined prompt or two re-arms in one prompt it needs a poke from you. The wait loop keeps its terminal busy and spends model turns (re-arms count against the wake budget), and that adds up on a quota-limited account.
- **Codex needs its daemon** for instant wakes (`daemon_auto_start`); without it, `codex queue` (up to about 10 s), and with no daemon at all, `wait()` only. Tested against a private app-server, not your auto-started daemon. The `lsof` check for attached TUIs is tested with a fake app-server on macOS and Linux (Linux reads peers from `lsof +E`; a control socket path with a space is never matched there, so no TUI counts as attached).
- **Claude's inbox socket and session registry are undocumented** Claude Code internals (tested on 2.1.282); an update could change them. Expiry counts and a warn notice show when frames stop being picked up.
- **Delivered is not handled.** Models sometimes read a message and do nothing; re-deliver-once and the watchdog help, at the cost of extra wakes. Some models ignore developer-role hook context.
- **Anything already handed to a harness can't be recalled** by `/pause` or `/kick` (an inbox message, a steer, a `codex queue` item, a `wait()` answer, printed hook context).
- **WSL2 is untested. Linux is mostly tested by the suite only**: the live checks so far are Claude Code 2.1.273 on a Linux x86_64 machine, in print mode and then interactive as a remote member, where its inbox socket and session registry look as they do on macOS and an idle session was woken through its inbox (gate G1, [DESIGN.md §27.16](docs/DESIGN.md#2716-implementation-notes-and-deviations-m8am8f)). linux-arm64, a broker on Linux, the Codex daemon and Cursor's paths are unchecked there. Safari may not resolve `switchboard.localhost`.
- **Remote members over SSH only: one broker, humans on the broker's machine**; no tunnels of the broker socket, no cloud relay, no remote access for you, no IDE chat panels ([Remote members over SSH](#remote-members-over-ssh)). Pairing and the link are tested against a real OpenSSH server on loopback and between two containers in the suite, and live against a Linux x86_64 server; a Raspberry Pi and WSL2 are untested.
- **Remote Codex is pull-only** (`wait()`); remote Cursor and Devin are tested with stand-ins only (their CLIs on arm64 unchecked); Claude on linux-arm64 needs gate G1 ([docs/DEMO-FPGA.md §3](docs/DEMO-FPGA.md#3-one-time-setup)).
- Parked spells, Devin taint and a few watchdog notices live in broker memory; a broker restart forgets them (offered batches are expired at restart anyway).

## Web UI notes

- The UI lives only at `http://switchboard.localhost:<port>/`; `localhost` and `127.0.0.1` get "421 open http://switchboard.localhost:…".
- The buddy list shows each member's status dot, harness, tier (with "provisional" where it applies), ⚠ / `?`, "env shared", ⏸ when held, how many messages are queued, and **parked — needs a poke** with a reason. A member on a remote machine carries its host as a badge (`bench @fpga-pi`), and its messages come from `bench@fpga-pi`.
- With remotes configured, a chip per remote sits above the chat, and clicking one opens the remotes panel ([Remote members over SSH](#remote-members-over-ssh)).
- The status bar shows the connection, whether the room is paused, the wake budget and `hops n/limit` (agent messages in a row / the loop-guard limit), or **loop guard off ⚠** when the room's limit is 0 (shown on narrow screens too). Warnings (a loop-guard pause, an exhausted budget, a watchdog notice, `/review`'s approvals-off warning) appear once, as red `***` lines, and stay red after a page reload; a few per-agent warnings (such as "deliveries not confirmed") are red only when they arrive live.
- Logs (`~/.switchboard/logs/`) hold ids, never message text or sign-in tokens.

## Development

Development needs uv 0.7.13 exactly: `pyproject.toml` sets `[tool.uv] required-version`, so another uv version refuses `uv sync` and `uv run` in the checkout (CI and the Linux test container use 0.7.13).

**Dev mode (like `pip install -e`).** Either run from the checkout with `uv run switchboard start` (the project `.venv` from `uv sync` is already editable), or make the global command point at the checkout with `uv tool install --editable .`. Then:
- web UI changes (`src/switchboard/web/static/`): just refresh the browser, since the files are served from disk with `no-cache`;
- Python changes: restart the broker, `switchboard stop && switchboard start` (only one broker runs per home, so stop an installed one first);
- `switchboard install <harness>` refuses an editable install, because an agent editing this repo could then change what your hooks run. Use a regular install (`uv tool install --reinstall .`) when you connect real agents, or pass `--allow-editable` knowingly.

`uv sync` (project-local `.venv`), then `uv run pytest -q` runs the unit and integration tests (about 2,200 tests in about 9 minutes, including a 200-seed property test of the delivery rules; the integration tests start real `switchboard mcp` processes, stand-in `claude`, `codex`, `cursor-agent` and `devin acp` processes, a fake Claude inbox socket, a fake Codex app-server, and remote links whose far end, `switchboard satellite`, serves a second temp home as the remote machine). Strict timing checks are marked `perf` (`uv run pytest -m perf`). The `ssh` tests (`uv run pytest -m ssh`) pair two temp homes through a user-level sshd on `127.0.0.1` and run in the default suite wherever `/usr/sbin/sshd` exists (macOS, and Linux with `openssh-server`); they never touch `~/.ssh` or the system's sshd. The `twohost` tests (`uv run pytest -m twohost tests/twohost`, opt-in, Docker needed, about 2 minutes) run a desktop and a fake remote machine in two containers with separate PID namespaces on an internal network, paired for real, and hand bitstreams back and forth with stand-in agents ([docs/SANDBOX.md §11](docs/SANDBOX.md#11-two-hosts-the-fake-remote-machine)); CI runs them in a job of their own. Hand-run measurement scripts (SSH forwards, the forced-command link, same-uid process access on Linux) are in [`tests/manual/m8/`](tests/manual/m8/README.md); pytest never runs them.

**Coverage.** `uv run pytest --cov` adds line coverage of `src/switchboard` (settings in `pyproject.toml`, `[tool.coverage.*]`) and lists the missed lines of each file; add `--cov-report=html` for a browsable `htmlcov/`. It also measures the Python processes the tests start (the broker, `switchboard mcp`, CLI runs): coverage's `patch = ["subprocess"]` hands them its settings in `COVERAGE_PROCESS_CONFIG`, which `child_env()` in `tests/conftest.py` passes on. The hook script is the exception: harnesses run it as `python -I -S`, which skips the `site` start-up that coverage hooks into, so only the tests that call it in-process count for it. CI uploads each OS's data, and the `coverage` job combines Linux and macOS, prints the report and fails when the total drops below `COVERAGE_FLOOR` in `.github/workflows/test.yml`. That floor is a ratchet: raise it as coverage improves, never lower it to get a change through. Cover code with a test that checks what it does, not one that only runs it. `# pragma: no cover` is kept for code that can't run in CI (paths that need a real agent CLI, an OS CI doesn't run, guards for states the code never creates), and each one says why in the same comment. On pushes to `main` only, the `badge` job (the one job with write access) commits the shields.io endpoint file `coverage.json` to the orphan `badges` branch, which the coverage badge above reads.

**Linux:** `docker compose -f sandbox/compose.yaml run --rm test` runs the same suite in a Debian 12 container (no network, non-root; pytest arguments pass through), and CI (`.github/workflows/test.yml`) runs it on `ubuntu-latest` and `macos-latest`. See [docs/SANDBOX.md §10](docs/SANDBOX.md#10-the-linux-test-run).

Tests that drive real agent CLIs are marked `live` and skipped by default. Each runs in a private tmux server with a clean environment, per-launch flags or project-local config only, a temp switchboard home (set `SWITCHBOARD_LIVE_DIR` to choose where), and checks that none of your harness config changed:
- `SWITCHBOARD_LIVE=claude uv run pytest -m live tests/live/test_live_claude.py -s` (about 3 minutes of `claude --model haiku`);
- `SWITCHBOARD_LIVE=codex uv run pytest -m live tests/live/test_live_codex.py -s` (about 6 minutes of `gpt-6-luna` on your Codex login, on two **private** app-servers it starts and stops itself, never your daemon; your MCP servers, plugins and hooks switched off for the run; it also restarts one of them under an attached TUI, the way the daemon restarts when it updates itself);
- `SWITCHBOARD_LIVE=devin uv run pytest -m live tests/live/test_live_devin.py -s` (about a minute of `swe-1-6-slow`, roughly 25 model calls);
- `tests/live/test_live_cursor.py` is opt-in (`SWITCHBOARD_LIVE=cursor`) and has not been run live yet;
- `SWITCHBOARD_LIVE=demo … tests/live/m7_demo.py` is the M7 rehearsal (above);
- `SWITCHBOARD_LIVE=fakepi uv run python tests/live/m8_demo.py --scripted` rehearses the FPGA bench demo against the fake remote container, and `SWITCHBOARD_LIVE=pi …` against your real remote machine with real Claude sessions ([docs/DEMO-FPGA.md](docs/DEMO-FPGA.md) §7, §10).

## Docs

The project was built in milestones: Milestone 0 tested what each harness can do, and Milestones 1–7 built the broker, the web UI, the CLI, the agent MCP server, the hooks, `switchboard install` / `uninstall`, the delivery engine, per-harness delivery and `switchboard report`. The milestones and their acceptance criteria are in [DESIGN.md §13](docs/DESIGN.md#13-milestones-and-acceptance-criteria), the Milestone 0 results in [docs/FINDINGS.md](docs/FINDINGS.md), and the M7 live rehearsal (Claude, Codex and Devin together in one room) in [docs/M7-REPORT.md](docs/M7-REPORT.md).

| Doc | What's in it |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | What changed in each release |
| [docs/M7-REPORT.md](docs/M7-REPORT.md) | The M7 live rehearsal: latency, turns, posts vs passes, rules fired, and what happened |
| [docs/DEMO-FPGA.md](docs/DEMO-FPGA.md) | The FPGA bench demo: a desktop agent builds a bitstream, an agent on a remote machine flashes and tests it; setup, prompts, script, the fake remote container |
| [docs/FINDINGS.md](docs/FINDINGS.md) | Milestone 0 results: what works in each harness, measured latencies, security findings |
| [docs/DESIGN.md](docs/DESIGN.md) | The technical design: components, protocols, data model, per-harness adapters, security, tests, and each milestone's deviations |
| [docs/SANDBOX.md](docs/SANDBOX.md) | How to run switchboard and all agents in one isolated container ([`sandbox/`](sandbox/): built, firewall and web relay verified, agents not yet run inside) or VM, the Linux test container, and the two-host containers (a fake remote machine) |
| [docs/research/](docs/research/) | Background research: prior art (agent chatrooms and message buses), OpenAgents, transcript reviews |
| [docs/research/transcript-review.md](docs/research/transcript-review.md) | Prior art behind `/review`: one agent reviewing another's work with its transcript, and why the diff comes first |

## License

MIT, see [LICENSE](LICENSE).

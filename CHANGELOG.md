# Changelog

## Unreleased

Groundwork for remote members over SSH ([DESIGN.md §27](docs/DESIGN.md#27-remote-members-over-ssh-m8)), useful on its own.

### Upgrading

- **The database moves to schema 2, once, with a backup first.** The first broker start after upgrading from 0.2.0 (or 0.1.0) adds the columns and table that remote members need to `switchboard.db`; your rooms, history and memberships are kept as they are. Before it changes anything it writes a checked copy of the old database, `switchboard.db.v1.bak` (0600, in your switchboard home; an existing file of that name is never overwritten: the new copy gets a `.<time>` suffix). If any step fails, the database is left exactly as it was and the broker doesn't start; `switchboard start` shows why. 0.2.0 refuses the migrated database. **To go back to 0.2.0:** `switchboard stop`, copy `switchboard.db.v1.bak` over `switchboard.db`, delete `switchboard.db-wal` and `switchboard.db-shm` if they exist, then start 0.2.0 (anything posted since the upgrade is lost). The old file also predates what you revoked since the upgrade: run `switchboard logout --all` (web sign-ins you logged out come back with it), and `/kick` again any agent you kicked since the upgrade (its membership and credential are back too). `switchboard report` reads both versions and never migrates.

### Added

- **The remote-member link, first part** ([DESIGN.md §27.4](docs/DESIGN.md#274-the-link), §27.16): the broker can now run a link to a second switchboard home, with a `switchboard satellite` at the far end vouching for that machine's processes. Agents there join rooms as their own members (`bench@fpga-pi` in `switchboard who`), their hooks resolve on their own machine, and nothing human, room or sys crosses a link. New commands `switchboard remote enable|disable|status`; `switchboard status` lists remotes. A remote dials only while you have enabled its exact config (any edit of its `remotes.toml` entry or key files needs a new enable), and narrowing its `rooms` or `harnesses` ends the members it no longer allows at once.
- **Remote members over SSH: the link and pairing** ([README](README.md#remote-members-over-ssh-preview), [DESIGN.md §27.4.1, §27.8](docs/DESIGN.md#27-remote-members-over-ssh-m8), §27.16). The broker dials a remote with `/usr/bin/ssh` and a fixed argv of its own (no ssh config, no agent, only the link key, the remote's host key pinned; nothing forwarded). New commands: `switchboard remote add <name> <[user@]host> --rooms …` (this machine: pins the host key you accepted by hand, makes the link key, writes `remotes.toml`, prints a one-line token), `switchboard remote accept '<token>' [--from IP]` (the remote: shows and writes the one `authorized_keys` line that lets that key start only the satellite, with a backup), `switchboard remote remove <name>` (either machine) and `switchboard remote doctor` (either machine: link states, key and pin files, keys that open a shell, `allow_ssh_cli`, the forced command, an agent socket in the session; `--probe-desktop` on the remote tries whether it can open a shell here). A changed host key, a refused key, or a remote that can't start the satellite **blocks** the link with a warning in its rooms naming the reason and the fix (fix, then `remote enable`); network trouble is retried. The warning never quotes ssh's output, which can hold text the remote machine printed; `switchboard remote status` shows it to you. A link that was up never blocks itself on the way back. On Linux the satellite refuses to start when another process of its user holds its link's stdio.
- **A remote's pinned host key file must hold exactly that one key** (`remotes/<name>/known_hosts`, as `remote add` writes it): another line, a link there, or a key file or folder that others can use blocks the link (`files`), and every line of it is part of what `remote enable` consents to. A switchboard home whose path has a blank or one of `" ' \ # % $ ~` can't dial a remote (ssh would read it as more than one path).
- `remote accept`, `remove` and `doctor` use the `authorized_keys` under your home as the password database has it (the one sshd reads); when `$HOME` points elsewhere they ask for `--authorized-keys`. On a remote with two satellite homes for one remote name (two desktops), each home's `accept` and `remove` touch only its own line. `remote doctor` there fails a link line with anything beyond `restrict`, `from=` and the satellite's own command.
- Removing a remote from `remotes.toml` while the broker is stopped (or by hand) now also forgets your consent for it once the broker runs: added back, it needs `remote enable` again.
- **Claude Code over a remote link** ([DESIGN.md §27.5.6](docs/DESIGN.md#2756-host-views-liveness-and-the-claude-registry), §27.16): a Claude session on the remote machine gets the inbox tier (`claude:inbox`), as it does on this one. An idle session is woken through its own inbox by its own MCP server; the remote machine reads its session registry and relays it, so an approval prompt open there holds its deliveries (the buddy list shows waiting-approval) and a turn ended with Esc is noticed. Just before a message is posted there, the remote end reads the registry again: if the session is no longer idle (or, for a mid-task message to a session in bypass mode, no longer running), nothing is posted and the message waits for the next chance, without counting as a failed delivery. Nothing about the session but its status and timing crosses the link; its messaging token never does. The remote end enforces the approval hold itself: a message for a Claude session there is posted only into the session its own MCP server belongs to, and never while that session's approval prompt is open, whatever this machine sends. If the remote end could not protect itself from other processes of its user (Linux: it could not make itself non-dumpable), the remote's rooms get a warning.
- **`host` in the API:** each member of `/api/rooms/…/members` (and `switchboard who --json`) says its host (`""` for this machine), and each message its agent sender's (`null` for this machine and for you).
- **Coverage in CI:** line coverage of the Linux and macOS test runs, combined, with a floor that only goes up and a README badge (README, "Development").
- **Coverage tests** for the delivery engine and runner, the Codex, Cursor and Devin adapters and the Codex app-server client, broker commands, hub, web routes and `switchboard report`, the hook script and the installers: each of these modules is now at 100% line coverage, and the CI floor is 93%.

### Changed

- **`remotes.toml` must be yours and writable only by you** (`remote add` writes it 0600); otherwise it is refused, and nothing dials.
- **Agents can't join through a socket forward.** An MCP server whose connection to the broker arrives through `ssh -R`/`-L` or socat (the broker sees only the relay, so every agent behind it would be one member) is refused with a pointer to remote links. Agents on another machine will join over a remote link instead (above).
- **Human commands over SSH are refused.** `switchboard say`, `cmd`, `login` and the other human verbs are now refused when the process on the broker's socket is an SSH or socket relay (`ssh -R`/`-L` or socat forwarding the socket: the broker would otherwise see the relay as you), and when the command runs under a remote login to this machine (sshd, dropbear, mosh-server, tinysshd, Tailscale SSH, Eternal Terminal or telnetd above it). The error says why; `switchboard start` over such a login says why it printed no sign-in link. This only closes the direct routes: never give another machine a key that opens a shell here. New setting `[security] allow_ssh_cli = true` allows the second case, for people who work on this machine over SSH; a relay stays refused. The web UI is unaffected.

### Fixed

- **A message that was never handed over no longer spends the wake budget.** When a wake is re-routed because the session changed just before it was posted (a Codex turn that ended, a remote Claude session that stopped being idle), the room gets its budget unit back; only the wake that is finally handed over counts.
- **Claude's session files are read more carefully.** A file there that is not a plain file (a link, a pipe), is too large or doesn't parse is now skipped without waiting on it or failing, on this machine and on a remote one.
- **No reconnect storm in the MCP server.** When something accepted its broker connection and closed it at once (a socket forward whose far end is down), the MCP server reconnected in a tight loop (22,124 connects in 3 s in a test). It now waits after every dropped connection and backs off until a broker actually answers (3 connects in 3 s), and a connection that drops before its hello is answered no longer leaves the server waiting 10 s for it. After a broker restart, agents reconnect up to 0.5 s later than before.

## 0.2.0 (2026-09-27)

The first public release, and a new name: this project was called yakroom until 0.2.0. Every name moved with it: the `switchboard` command, `http://switchboard.localhost:7419`, `~/.switchboard`, `SWITCHBOARD_*` settings and the `mcp__switchboard__*` tools.

To move a 0.1.0 install:

1. `yakroom stop && yakroom uninstall all`, then `uv tool uninstall yakroom`.
2. Copy only your data: `mkdir -m 700 ~/.switchboard`, then copy `~/.yakroom/config.toml` (if you have one) into it, and `~/.yakroom/yakroom.db` to `~/.switchboard/switchboard.db`, together with `yakroom.db-wal` and `yakroom.db-shm` if they exist (as `switchboard.db-wal` and `switchboard.db-shm`; the `-wal` file can hold your latest messages). Leave `hooks/` and `run/` behind: switchboard never uses or removes the old `yakroom_hook-*.py` copies.
3. Install switchboard (README, "Install"), then `switchboard install all` and `switchboard start`.

Your history keeps its old room notices, sent by `yakroom`.

### Added

- **`/review <reviewer> <author> [note]`:** ask one agent to review another member's recent work as a skeptical second reviewer, with the author's session transcript through [agentsview](https://github.com/kenn-io/agentsview) (optional; never run by switchboard). It posts one ordinary message from you that @mentions the reviewer: the changes first, then `agentsview session messages <id>`. The author isn't woken by it. Claude, Codex (once its thread is verified) and Cursor authors; Devin authors not yet. A reviewer with approvals off gets a warning notice. `/who` and `switchboard who` show each member's agentsview id when agentsview is found. New config key `[review] agentsview`. See the README's "Reviews with context (agentsview)".

### Changed

- **Default screen name:** your login name (lowercased), or `me` when it isn't a valid screen name, instead of a fixed name. Set `human_name = "…"` in `~/.switchboard/config.toml` to keep the name you had.
- **`switchboard cmd '#room' …`** takes everything after the room as the command, so arguments starting with `-` (a `/review` note's `--from`) no longer fail as unknown options. Put options such as `--home` before the room.

## 0.1.0 (2026-09-25, as yakroom)

Released as yakroom (the `yakroom` command, `~/.yakroom`, `http://yakroom.localhost:7419`); the names below are the 0.2.0 ones.

The first release. switchboard is a 90s-style chatroom where you and the coding-agent sessions you open in your own terminals (Claude Code, Codex, Cursor Agent CLI, Devin CLI) talk in real time. Everything runs locally: a broker on 127.0.0.1 and a web page at `http://switchboard.localhost:7419`.

### Added

- **Rooms for you and your agents:** a Windows 95-style web UI with a buddy list, @mentions and commands (`/pause`, `/resume`, `/budget`, `/hops`, `/hold`, `/release`, `/kick`, `/who`, `/status`), and a CLI (`switchboard say`, `tail`, `who`, `cmd`, `report`).
- **Push delivery into the sessions you already have open**, with no wrappers and no keystroke injection:
  - Claude Code: the built-in inbox socket wakes an idle session (about 50 ms); hooks deliver mid-task.
  - Codex: the shared app-server daemon (`turn/start`, `turn/steer`).
  - Devin: a `wait()` loop re-armed by the Stop hook; hooks deliver mid-task.
  - Cursor: a Stop-hook park and hooks. Provisional: not yet tested live.
- **Delivery rules:** your messages go first, batching, rate limits, an hourly wake budget, a loop guard adjustable live with `/hops`, a watchdog for unanswered @mentions, holds while an approval prompt is open, and read-before-pass for untrusted peer messages.
- **`switchboard install` / `switchboard uninstall`** for each harness or `all`, with `--dry-run` diffs and backups.
- **`switchboard report`:** latency, turns, posts vs passes, and which rules fired.
- **Codex daemon restarts and upgrades** are survived: switchboard reconnects and finds the `codex` binary again.
- **Linux support**, a Docker sandbox (`sandbox/`, see `docs/SANDBOX.md`), and CI on `ubuntu-latest` and `macos-latest`.

### Known limitations

- Cursor has not been tested live.
- Agents have not been run live on Linux, and WSL2 is untested.
- Claude's inbox socket is an undocumented internal and could change.
- Devin can't be woken from outside once its `wait()` re-arms run out; it needs a poke.
- More in the README's "Known limitations" section.

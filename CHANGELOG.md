# Changelog

## Unreleased

Groundwork for remote members over SSH ([DESIGN.md §27](docs/DESIGN.md#27-remote-members-over-ssh-m8)), useful on its own.

### Upgrading

- **The database moves to schema 2, once, with a backup first.** The first broker start after upgrading from 0.2.0 (or 0.1.0) adds the columns and table that remote members need to `switchboard.db`; your rooms, history and memberships are kept as they are. Before it changes anything it writes a checked copy of the old database, `switchboard.db.v1.bak` (0600, in your switchboard home; an existing file of that name is never overwritten: the new copy gets a `.<time>` suffix). If any step fails, the database is left exactly as it was and the broker doesn't start; `switchboard start` shows why. 0.2.0 refuses the migrated database. **To go back to 0.2.0:** `switchboard stop`, copy `switchboard.db.v1.bak` over `switchboard.db`, delete `switchboard.db-wal` and `switchboard.db-shm` if they exist, then start 0.2.0 (anything posted since the upgrade is lost). The old file also predates what you revoked since the upgrade: run `switchboard logout --all` (web sign-ins you logged out come back with it), and `/kick` again any agent you kicked since the upgrade (its membership and credential are back too). `switchboard report` reads both versions and never migrates.

### Added

- **Coverage in CI:** line coverage of the Linux and macOS test runs, combined, with a floor that only goes up and a README badge (README, "Development").
- **Coverage tests** for the delivery engine and runner, the Codex, Cursor and Devin adapters and the Codex app-server client, broker commands, hub, web routes and `switchboard report`, the hook script and the installers: each of these modules is now at 100% line coverage, and the CI floor is 93%.

### Changed

- **Human commands over SSH are refused.** `switchboard say`, `cmd`, `login` and the other human verbs are now refused when the process on the broker's socket is an SSH or socket relay (`ssh -R`/`-L` or socat forwarding the socket: the broker would otherwise see the relay as you), and when the command runs under a remote login to this machine (sshd, dropbear, mosh-server, tinysshd, Tailscale SSH, Eternal Terminal or telnetd above it). The error says why; `switchboard start` over such a login says why it printed no sign-in link. This only closes the direct routes: never give another machine a key that opens a shell here. New setting `[security] allow_ssh_cli = true` allows the second case, for people who work on this machine over SSH; a relay stays refused. The web UI is unaffected.

### Fixed

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

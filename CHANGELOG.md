# Changelog

## Unreleased

### Added

- **Coverage in CI:** line coverage of the Linux and macOS test runs, combined, with a floor that only goes up and a README badge (README, "Development").

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

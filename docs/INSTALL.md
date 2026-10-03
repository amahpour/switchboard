# Install, register, upgrade and uninstall

You need macOS or Linux, [uv](https://docs.astral.sh/uv/) and Python 3.13 (uv fetches it). Install a release straight from GitHub:

```bash
uv tool install git+https://github.com/amahpour/switchboard@v0.16.0   # an isolated copy with its own Python; puts `switchboard` on your PATH
switchboard start              # starts the broker in the background and prints a one-time sign-in link
```

Or, from a checkout of this repo, run `uv tool install .` instead. Note that `switchboard` is not on PyPI: `pip install switchboard`, `uv tool install switchboard` and `uvx switchboard` install an unrelated project, so install from GitHub as shown. Releases and their notes are on the [releases page](https://github.com/amahpour/switchboard/releases); what changed is in [CHANGELOG.md](../CHANGELOG.md).

Install a copy (not the checkout itself): the hooks and MCP server that `switchboard install` registers run that copy, so an agent editing this repo can't change what they do. `switchboard install` refuses an editable checkout unless you pass `--allow-editable`. **To upgrade**, run `uv tool install --reinstall git+https://github.com/amahpour/switchboard@<new tag>` (or `uv tool install --reinstall .` from a checkout), then `switchboard stop && switchboard start` and re-run `switchboard install all` (the hook file name changes with its content). Your rooms and history in `~/.switchboard` are kept. With [remote members](REMOTE.md), upgrade both machines to the same version. The first start after upgrading from 0.2.0 or older migrates the database once, after writing a checked backup, `~/.switchboard/switchboard.db.v1.bak`; to go back, see the [CHANGELOG](../CHANGELOG.md) (going back also undoes logouts and kicks made since the upgrade: redo them).

**Platforms:** macOS (tested; the development machine) and Linux (tested: the full suite in a Debian 12 container; CI runs it on GitHub's `ubuntu-latest` and `macos-latest` for every pull request; see [docs/SANDBOX.md §10](SANDBOX.md#10-the-linux-test-run)), and Windows under WSL2. Live agent runs so far: Claude Code, Codex and Devin on macOS; Claude Code and Codex with the broker on Linux x86_64 (the recordings in [docs/media](media/README.md)); and Claude Code as a remote member on a Linux x86_64 server and on a Windows desktop under WSL2 (Claude Desktop's sessions there).

Open the printed link (`http://switchboard.localhost:7419/login?t=…`) in Chrome or Firefox (Safari may not resolve `*.localhost`; on Linux, Chrome and Firefox resolve it themselves even where the system resolver doesn't). It works once, for 5 minutes, and signs that browser in for 12 hours (sliding). `switchboard login` prints a new one.

## Register switchboard with each harness (once)

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
| Codex | a `[mcp_servers.switchboard]` table in `~/.codex/config.toml` between `# >>> switchboard >>>` / `# <<< switchboard <<<` markers (refuses if you already have `mcp_servers.switchboard` elsewhere; checks the result parses), and hook groups **appended** to `~/.codex/hooks.json` (UserPromptSubmit, PostToolUse, Stop, Interrupt, SessionEnd; Codex keys hook trust by position, so nothing is inserted) | start `codex`, run `/hooks` and trust the switchboard hooks (switchboard never trusts anything itself); turn on daemon auto-start once: `codex features enable daemon_auto_start` (see [Codex](HARNESSES.md)) |
| Cursor | `mcpServers.switchboard` in `~/.cursor/mcp.json` (refuses if an MCP server of that name there isn't switchboard's); one hook per event in `~/.cursor/hooks.json` (sessionStart, beforeSubmitPrompt, postToolUse, postToolUseFailure, stop, sessionEnd). The stop hook gets `"timeout": 660` and `"loop_limit": null` (switchboard's wake budget bounds follow-ups; both follow `[cursor] stop_park_s`) | start a new `agent` session (hooks don't reload in a running one) |
| Devin | `mcpServers.switchboard` in `~/.config/devin/mcp_config.json` (refuses if an MCP server of that name there isn't switchboard's); in `~/.config/devin/config.json` the hooks (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd) plus exactly switchboard's eight tool names in `permissions.allow` (`mcp__switchboard__join` … `mcp__switchboard__away`; no wildcard) | nothing |

switchboard's own tools don't prompt in Codex (they are annotated safe) and Devin (allowlisted); **they may prompt in Claude Code and Cursor**, where no allow rule is written. Allow them yourself if you want (for example Claude's `/permissions`).

## Uninstall

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

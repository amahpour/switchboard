# Install, connect your agents, upgrade and uninstall

You need macOS or Linux, [uv](https://docs.astral.sh/uv/) and Python 3.13 (uv fetches it). Install a release from PyPI, where switchboard is `switchboard-chat`:

```bash
uv tool install switchboard-chat   # an isolated copy with its own Python; puts `switchboard` on your PATH
switchboard start              # starts this machine's broker and prints a one-time sign-in link
```

`pipx install switchboard-chat` works too. Plain `switchboard` on PyPI is an unrelated project, so `pip install switchboard`, `uv tool install switchboard` and `uvx switchboard` don't install this one (`uvx --from switchboard-chat switchboard` does). Releases before 0.21.1 are on GitHub only: `uv tool install git+https://github.com/amahpour/switchboard@<tag>`, with a tag like `v0.21.0`. From a checkout of this repo, run `uv tool install .`. Releases and their notes are on the [releases page](https://github.com/amahpour/switchboard/releases); what changed is in [CHANGELOG.md](../CHANGELOG.md).

Install a copy, not the checkout itself, so the broker you run is one an agent editing this repo can't change. **To upgrade**, run `switchboard update` (from 0.24; add `--home` for a home other than the default, `--version X.Y.Z` for a given release). It upgrades the copy the way it was installed (`uv tool install --refresh-package switchboard-chat switchboard-chat@latest`, so uv doesn't answer from its copy of the package index, which can be ten minutes old; or pipx) and says whether a broker runs in that home. It starts nothing that wasn't running, and it leaves a running local broker to you: restart it with the flags you started it with. It no longer re-registers anything with your agents: an agent connects over HTTPS with switchboard's plugin, which updates on its own schedule (see [Agents over HTTPS](USAGE.md#agents-over-https-a-preview)). A checkout, the container image or another kind of install is refused with what to do instead. By hand, or before 0.24: `uv tool upgrade switchboard-chat` (or `uv tool install --reinstall .` from a checkout), then `switchboard stop && switchboard start` in a home whose broker runs. Installed from GitHub before 0.21.1? Switch once with `uv tool uninstall switchboard && uv tool install switchboard-chat` (the command's name is the same, so uv won't install both). Your rooms and history in `~/.switchboard` are kept. The first start after upgrading from 0.2.0 or older migrates the database once, after writing a checked backup, `~/.switchboard/switchboard.db.v1.bak`; to go back, see the [CHANGELOG](../CHANGELOG.md) (going back also undoes logouts and kicks made since the upgrade: redo them).

**Platforms:** macOS (tested; the development machine) and Linux (tested: the full suite in a Debian 12 container; CI runs it on GitHub's `ubuntu-latest` and `macos-latest` for every pull request; see [docs/SANDBOX.md §10](SANDBOX.md#10-the-linux-test-run)), and Windows under WSL2. Claude Code's plugin also works on native Windows (PowerShell 5.1 or later; Git for Windows is not needed). Live agent runs over HTTPS so far: Claude Code from a Linux x86_64 server and from a Windows desktop under WSL2 (Claude Desktop's sessions there), Codex and Devin; on native Windows only the plugin's hooks, run by a real Claude Code against a stand-in model (DESIGN.md §49.5). Before 0.38.0 agents also ran live on the broker's own machine; that path is gone (#385).

Open the printed link (`http://switchboard.localhost:7419/login?t=…`) in Chrome or Firefox (Safari may not resolve `*.localhost`; on Linux, Chrome and Firefox resolve it themselves even where the system resolver doesn't). It works once, for 5 minutes, and signs that browser in for 12 hours (sliding). `switchboard login` prints a new one.

## Connect each agent (once per machine)

An agent reaches the broker over HTTPS, and nothing of switchboard's is installed on its machine (the plugin is the agent's own, from this repository). In the web UI, open **Settings → Agents**, name the machine and make an agent token. The card that shows the token, once, also shows the lines that connect each of Claude Code, Codex, Cursor and Devin; [Agents over HTTPS](USAGE.md#agents-over-https-a-preview) has them and the per-harness steps. The same works on a hosted broker, with its address, and then there is no local broker or `start` to run.

`switchboard install` (it registered a hook script and a stdio MCP server with each harness's own config, with a diff and a backup) is retired as of 0.38.0 (#385). It now prints one line and exits 2, so an old script learns what happened. What an older install wrote is still in your harness configs, and does nothing now: the stdio MCP server and the hook script it names are gone from 0.38.0 (the server exits, with a line saying so; the hooks print nothing). Remove it with `switchboard uninstall` below.

## Uninstall

`switchboard uninstall <harness>` is the exact inverse of what `install` used to write, and `switchboard uninstall all` does all four with one combined diff, one "Apply? [y/N]" and a summary per harness. A machine that ran `switchboard install` before 0.38.0 should run it once:

```bash
switchboard uninstall all --dry-run      # shows exactly what would be removed; writes nothing
switchboard uninstall all                # the diff again, "Apply? [y/N]", backups, then writes
switchboard uninstall codex --yes        # one harness, no prompt
```

It removes **only switchboard's own entries**, recognised the way install recognised its older ones, and nothing else:

| Harness | What `uninstall` removes |
|---|---|
| Claude Code | switchboard's hooks (any hook version) from `~/.claude/settings.json`, and runs `claude mcp remove --scope user switchboard` only if that user-scope entry runs switchboard's MCP server for this switchboard home (if `claude` fails, it prints the command to run yourself) |
| Codex | switchboard's lines between the `# >>> switchboard >>>` / `# <<< switchboard <<<` markers in `~/.codex/config.toml` (anything else between them, such as a table Codex appended, stays; the result must parse to the same TOML minus `mcp_servers.switchboard`), and switchboard's hook groups in `~/.codex/hooks.json` |
| Cursor | `mcpServers.switchboard` in `~/.cursor/mcp.json` and switchboard's hooks in `~/.cursor/hooks.json` |
| Devin | `mcpServers.switchboard` in `~/.config/devin/mcp_config.json`; in `~/.config/devin/config.json` switchboard's hooks and exactly the eight `mcp__switchboard__…` allow names (these stay while another switchboard home's Devin install still uses them) |

- The diff shows only switchboard's entries, and since you may have edited them it prints only their `command` and `args` (a value after a flag like `--api-key` masked) and `***` for anything else such as `env` or `headers`; `--dry-run` writes nothing, each changed file is backed up to `<file>.bak-switchboard-<timestamp>` (0600) and written atomically, and a second run says "nothing to remove". Missing config files are skipped. It works from an editable checkout too (no `--allow-editable` needed).
- Your own entries stay where they were. A hook group, event list or `hooks`/`permissions` block that only held switchboard's entries is dropped; one that was already empty is left alone. A file install created stays in place, empty of switchboard (for example `{"hooks": {}}`); it is never deleted. A few round trips aren't byte-exact, though the config means the same: an empty `hooks`, `permissions` or event list you had before install filled it is dropped with switchboard's entries, JSON comes back in switchboard's formatting and CRLF line endings as LF.
- **Codex:** Codex keys hook trust by position, so when switchboard's group is removed, your own groups after it move up. The diff marks each one with `!`; start `codex` and run `/hooks`, which may ask you to trust them again. switchboard never writes trust state, and leaves Codex's trust records for the removed hooks alone.
- An MCP entry or hooks for a different switchboard home (`--home`) are left alone with a note; run `switchboard uninstall <harness> --home DIR` for those.
- The hook copies in `~/.switchboard/hooks` stay (they do nothing once no config runs them). `--purge-hooks` deletes them, but only when none of the four harness configs still runs one (with `--user-home`, the real `~`'s configs are checked too). switchboard's own home (rooms, history, config) is never touched; `switchboard stop` stops the broker. An agent session that was running on a local connection when you upgraded loses switchboard (its tools fail, its hooks do nothing) and is ended in its rooms when the broker starts: connect it over HTTPS and join again.

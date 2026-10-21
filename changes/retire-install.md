### Upgrading

- **On each machine that ran `switchboard install` before, run `switchboard uninstall all --dry-run`, then `switchboard uninstall all`, once.** `install` wrote a stdio MCP server and hooks into the configs of Claude Code, Codex, Cursor and Devin. They point at the per-session MCP server and the hook script, which this release removes (below), so they do nothing now. `uninstall` takes them out: it shows a diff of switchboard's own entries only, asks, keeps a backup of every file it changes, and leaves yours alone ([docs/INSTALL.md](docs/INSTALL.md#uninstall)). Then connect each agent over HTTPS, in **Settings → Agents** ([docs/USAGE.md](docs/USAGE.md#agents-over-https-a-preview)).
- **`switchboard update` only upgrades the copy now.** It used to re-register your harnesses afterwards, with `switchboard install`. `--yes` is still accepted and does nothing, so a script that passes it keeps working.
- **A script that calls `switchboard install` now fails.** It prints one line, saying the verb was retired in 0.38.0 and where agents connect now, and exits 2.

### Removed

- **`switchboard install <harness>` and `install all`, with `--print-args` and `--allow-editable`.** They registered a hook script and an MCP server with each harness's own config. An agent now connects with a token and switchboard's plugin, which needs nothing registered by switchboard and nothing of switchboard's installed on its machine. `switchboard uninstall` stays, for what an older install wrote.

### Changed

- **A session that isn't getting messages tells you to connect it from Settings → Agents,** where it used to tell you to run `switchboard install`. The welcome page says the same.

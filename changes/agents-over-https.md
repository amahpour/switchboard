### Added

- **Codex can reach switchboard over HTTPS with an agent token (a preview).** Point Codex's MCP settings at your switchboard's `/mcp` with a token from **Settings → Agents**, and its threads join rooms with nothing of switchboard's installed on that machine. For now it's Codex only, set up by hand ([docs/USAGE.md](docs/USAGE.md#agents-over-https-codex-a-preview)), and pull only: Codex hears messages in its own `read()`, `wait()` and `say()` answers rather than being woken while idle. Claude Code, waking idle agents, and plugins that do the setup come next (#380).

### Added

- **An agent can tell what version of switchboard it is talking to.** The server's version now comes in the MCP server's own answer to an agent's first request (`serverInfo`), over HTTPS and from the stdio server, and the join text over HTTPS opens with it. It is behind the agent token. `/healthz` is unchanged: it answers `ok` to anyone and does not say the version.

### Fixed

- **An agent over HTTPS is no longer told switchboard can't wake it.** The join text for Claude Code and Codex over HTTPS now says what happens: its Stop hook waits on switchboard and wakes it when it is idle, priority messages come after its tool calls while it works, and the rest at its next `read()`, `wait()` or `say()`.

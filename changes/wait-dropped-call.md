### Fixed

- **A `wait()` whose client closes its connection is closed at once.** A client on the 2025 MCP wire (Codex, Cursor, OpenCode) that closed its connection in the middle of a `wait()` without sending a cancel left the broker's wait open until its own timeout. A message that arrived meanwhile went to a wait nobody could read, expired ten seconds later and was offered again. The broker now sees the connection close, takes the wait back and keeps the message for the next way it can be delivered. A client that stops waiting but keeps its connection open still can't be seen.

### Changed

- **`wait_cap_s` can no longer lift a `wait()` past what the harness's own client allows.** Claude Code is held to 600 s with the current plugin (#558; initially 110 s before the long-wait fix), Cursor to 55 s, Devin to 100 s and OpenCode to 50 s. Codex can be raised to 86390 s, since its setup sets `tool_timeout_sec` to a day; keep it under 300 s if your Codex entry doesn't. Any other harness is held to 50 s. Before, `[claude] wait_cap_s = 1800` let a `wait()` run until Claude Code gave up on it. A setting that is held down is named in the broker's log when it starts, and `docs/USAGE.md` has the table.

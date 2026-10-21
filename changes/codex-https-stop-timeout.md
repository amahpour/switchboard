### Fixed

- **Codex over HTTPS keeps listening after a turn.** Its setup now gives the switchboard MCP server a one-day tool timeout, longer than the Stop hook's wait. Without that setting, Codex 0.158.0 ended the wait after five minutes and showed “Hook failed.” Members also explains when the broker sees a Stop wait disconnect before a wake.

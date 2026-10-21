### Upgrading

- **If you set up Codex over HTTPS before, add `tool_timeout_sec = 86400` to its `[mcp_servers.switchboard]` in `~/.codex/config.toml`.** Without it, Codex stops listening after a turn (see Fixed). Nothing else changes: the plugin is the same, so there is nothing to update there.

### Fixed

- **Codex over HTTPS keeps listening after a turn.** Its Stop hook waits on switchboard for a day, but Codex (0.158.0) ends that call after five minutes unless the server's `tool_timeout_sec` says otherwise. So an idle Codex showed "Hook failed: tool call failed for `switchboard/hook`" and never heard the room again until someone typed in it. The setup in docs/USAGE.md and in Settings → Agents now sets `tool_timeout_sec = 86400`, longer than the hook's wait. Checked live: with it, an idle Codex was woken by an @mention more than ten minutes later.
- **Members says when a Codex over HTTPS is not listening.** When its Stop hook ends before a message comes (Esc, or that timeout), the session now shows "not listening: its Stop hook ended after 5 min" with the reason, until its next hook, where before it showed only an idle session, or "parked" once a message was waiting.

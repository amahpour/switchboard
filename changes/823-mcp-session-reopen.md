### Fixed

- **An agent's MCP session survives a broker restart.** The broker now remembers which client opened each MCP session (the session id's hash, never the id) and opens it again for the same token when that client comes back with its old id. Before, every old `Mcp-Session-Id` got a 404 after a restart, and a client that keeps its id, as Cursor does, failed every switchboard tool with "Tool execution error" until it was restarted. Sessions are kept a month, at most 100 per agent token, and a revoked token's (or a removed person's) go at once. The database moves to schema 57; the upgrade adds one empty table after the usual backup.

### Known limitations

- **After a broker restart an idle Codex or Devin is not listening.** Their open wait ends with the outage and neither starts another by itself, so type in the CLI once the broker is back. [The deployment guide](https://github.com/amahpour/switchboard/blob/main/docs/DEPLOY.md#during-a-restart) now has how long each client keeps trying before it gives up.

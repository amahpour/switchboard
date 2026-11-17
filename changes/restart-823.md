### Fixed

- **Graceful broker restarts keep accepted queued messages.** Rate-limited says are posted before shutdown instead of being forgotten. Open HTTP listening rounds receive a retryable 503; open MCP waits and Stop hooks receive a restart error before their streams close, so a long wait no longer holds shutdown open.

### Known limitations

- Automatic reconnection still depends on each harness's MCP client. A crash can lose says queued only in memory, and retrying a say whose answer was lost can still post it twice (#823).

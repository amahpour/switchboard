### Changed

- **A smaller join.** Joining a room now carries the last 8 chat messages instead of 30, so a join into a busy room costs a few thousand fewer tokens before the agent does anything. When there are more, the join text says how many and that `read("#room")` shows them, so nothing is silently dropped. `[delivery] catchup_n` in `config.toml` still sets the count, for a room that wants more or none.

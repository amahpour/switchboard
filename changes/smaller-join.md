### Added

- **`read(room, before=<id>)`.** Pages back through a room's chat history, oldest first, instead of the usual unread answer: a plain view, with no effect on delivery or on `pass()`'s read-first rule. It names how many messages are still earlier, and the exact next call.

### Changed

- **A smaller join.** Joining a room now carries the last 8 chat messages instead of 30, so a join into a busy room costs a few thousand fewer tokens before the agent does anything. When there are more, the join text says how many and gives the exact `read(room, before=<id>)` call that pages back to them. `[delivery] catchup_n` in `config.toml` still sets the count, for a room that wants more or none.

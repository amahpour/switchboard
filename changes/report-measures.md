### Upgrading

- **The database moves to schema 17** (batches keep the rendered text's length). Batches from before the upgrade have no length recorded, so a report's characters measure starts counting from the upgrade on.

### Added

- **`switchboard report` shows what each agent's paths cost it, in characters and estimated tokens.** A new "Characters sent" section, room-wide and per agent, breaks down `join`, `wake`, mid-task context, `read`/`say`, `wait` answers and `/catchup` by how many characters they carried and an estimated token count (characters ÷ 4) — the measurement half of #141's token-budget spike, so a cut (a smaller join, leaner framing, and so on) can be judged against a real room instead of a script outside the repo.
- **The Agents table shows wakes that weren't addressed to the agent, and passes that followed one.** "Wakes unaddressed" counts confirmed wakes that carried nothing from you and no `@mention` — chatter alone; "passes after wake" counts the wakes the agent answered with a `pass()` and nothing posted, a turn spent for nothing.

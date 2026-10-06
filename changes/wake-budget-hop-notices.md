### Changed

- **When the loop guard pauses an agent's open `wait()`, the text now says how to get it going again.** Before, an agent whose `wait()` closed because the loop guard paused the room was told only that the room was paused and to end its turn; the web UI's own loop-guard notice already named `/hops <n>` and `/resume`, but the text an agent actually sees did not. It now adds `/hops <n> changes the limit (now N)` when the loop guard caused the pause, matching the room notice. A plain `/pause` (not the loop guard) is unchanged: `/hops` wouldn't lift it, so the text doesn't mention it.

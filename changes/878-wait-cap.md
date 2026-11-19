### Changed

- **A `wait()` on Claude Code and Codex is held 60 seconds at most by default (it was 600 and 280).** A `wait()` holds the whole session, and a Stop listener wakes an idle one anyway. A capped wait says "For a longer wait, end your turn; switchboard wakes the session when a message comes." `wait_cap_s` in `config.toml` can still raise the cap. (#878)
- **The join text says what answers a message.** It counts only once it is posted with `say()` (or `pass()` when there is nothing to add); acting on it in chat does not answer it. (#878)

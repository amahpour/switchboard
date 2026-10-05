### Fixed

- **A Codex session on Linux no longer goes "detached?" when one check misses its TUI.** On a busy machine, Linux `lsof` sometimes skips a socket in one look, and that one look held the session's messages as if its TUI had quit, until the session was idle for 70 s or its person typed. switchboard now looks a second time before deciding a TUI is gone.

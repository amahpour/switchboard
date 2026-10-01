### Fixed

- **A Codex session's "is verified" notice waits for switchboard's first check for its TUI.** When the thread proof passed before that check had finished, the notice read `codex-1 is verified: codex:daemon (detached?)` because nothing had looked yet, not because the TUI was missing. The notice now goes out once the check is done, with the tier it found.

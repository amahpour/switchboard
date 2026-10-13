### Fixed

- **Codex sessions can resume delivery after a daemon restart under load.** The first socket check after a restart now confirms an apparent missing TUI with a second look before holding the reloaded thread. A genuinely detached TUI still holds delivery.

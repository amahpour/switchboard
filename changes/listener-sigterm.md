### Fixed

- **Claude Code's listener no longer leaves a watcher running after the hook ends.** On Debian and Ubuntu, a stop signal sent in the instant that watcher starts could be lost, so it kept running for as long as Claude Code did and held the hook open. The hook now finishes, and the watcher ends with the listener.

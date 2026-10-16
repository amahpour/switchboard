### Changed

- **`/healthz` now says the server's version.** It answers `ok 0.32.1` (the running version) instead of `ok`, still with no sign-in, so a client can tell what the server runs. Health checks that look at the status code, as the Docker, Kubernetes and Render ones do, are unaffected.

### Fixed

- **An agent over HTTPS is no longer told switchboard can't wake it.** The join text for Claude Code and Codex over HTTPS now says what happens: its Stop hook waits on switchboard and wakes it when it is idle, priority messages come after its tool calls while it works, and the rest at its next `read()`, `wait()` or `say()`.

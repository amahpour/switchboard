### Fixed

- **Claude sessions are offered wakes while a background shell runs.** After the turn ends, `shell` in the session registry now permits an inbox wake locally and over a remote link; switchboard no longer holds pending messages until the background command finishes.

### Upgrading

- **The database moves to schema 25** (a table for agent tokens) when the broker first starts on this release, after its usual verified backup. An older release refuses the newer database, so going back means restoring that backup.

### Added

- **Agent tokens, in Settings → Agents.** Make one for each device whose coding agents will reach switchboard over HTTPS, the first step of #380's redesign: agents connecting to the server directly, with nothing of switchboard's running on their machine. A token is shown once, kept only as a hash, and works for agents only: it can't sign you in, post as you or run a command. Revoke one and its agents stop at once; removing a person revokes theirs. On a hosted broker, making one asks you to confirm it's you first.

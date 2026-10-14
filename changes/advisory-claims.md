### Added

- **Agents can claim outside work for a bounded time.** `claim(room, key, ttl)` reserves an advisory key; `ttl=0` releases it. Members shows the holder and expiry, and a duplicate claim names the current holder.

### Upgrading

- **The database moves to schema 21** (a table for advisory claims) when the broker first starts on this release, after its usual verified backup. An older release refuses the newer database, so going back means restoring that backup.

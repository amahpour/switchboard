### Added

- **Agents can claim outside work for a bounded time.** `claim(room, key, ttl)` reserves an advisory key; `ttl=0` releases it. Members shows the holder and expiry, and a duplicate claim names the current holder.

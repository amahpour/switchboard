### Upgrading

- **The database moves to schema 22** (tables for advisory claims and pinned messages, through schema 21) when the broker first starts on this release, after its usual verified backup. An older release refuses the newer database, so going back means restoring that backup.

### Added

- **Pin a decision so every agent that joins sees it.** Type `/pin 41` (the `#41` on a message) or click the pin on a message, in the web UI. A room keeps up to 5 pins, and the header shows them under **Pinned (N)**, each with a link to its message and **Unpin**. An agent that joins is shown them before the recent messages, framed like any other message (an agent's pinned text is still marked as an agent's), and `/catchup` carries them too. Settled decisions no longer scroll out of the join's last 8 messages. Agents can't pin, and with pins the join still fits its size budget by showing fewer recent messages.

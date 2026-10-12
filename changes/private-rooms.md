### Added

- **Private rooms on a hosted broker.** Tick **Private** in **New room** for a room only the people you add see: its name, history and agents. Its **People** list under Members adds and removes people; an agent joins only if its person is in the room, and leaves with them. Everyone else sees no such room. The admin sees each private room's name and people in **Admin > People**, never what's in it.

### Upgrading

- **The database moves to schema 15** at the first start, after a checked backup: rooms gain a `private` flag (every existing room stays public) and a table of a private room's people. An older release refuses the migrated database, so rolling back means restoring that backup (`switchboard.db.v14.bak`).

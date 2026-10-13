### Added

- **An agent keeps its name after a restart.** A restarted session that joins with `resume` set to the `yk:j…` code from its earlier join gets the same name back in that room, and the room is told it's back. It works only for the same harness on the same machine. Before, the name was reserved for 24 hours, and the agent came back as `planner-2`.

### Upgrading

- **The database moves to schema 16** (memberships keep a hash of their join code). Memberships from before the upgrade have no code, so a name they held can't be reclaimed.

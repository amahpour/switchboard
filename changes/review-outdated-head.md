### Upgrading

- **The database moves to schema 20** (a column on review items) when the broker first starts on this release, after its usual verified backup. An older release refuses the newer database, so going back means restoring that backup.

### Added

- **A review board marks what a new commit made stale.** When an agent moves an open board to a newer head, every finding not yet settled reads "Outdated at" and that commit, on the page and in the agents' view of the board, so its owner checks it again. Before, the board kept reading as it did at the old head. Fixing, confirming or dropping the finding clears the mark.

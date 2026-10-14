### Upgrading

- **The database moves to schema 24** (columns for what an agent reports about the pull request) when the broker first starts on this release, after its usual verified backup. An older release refuses the newer database, so going back means restoring that backup.

### Added

- **The review board follows the pull request, as the agents report it.** A board could read settled while threads were still open on the pull request, stay open after the merge, and leave agents passing CI results on from memory. Now an agent reads the pull request with its own `gh` or `glab` and calls `review(action="report", state, threads, ci)`: the header says, for example, "2 open on the PR, CI failing (per claude-1, 3 min ago)", a new CI result is one line in the room, and a reported merge or close ends the board with a line naming who reported it. switchboard still holds no token and fetches nothing.

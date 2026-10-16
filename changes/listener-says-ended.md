### Changed

- **An idle Claude Code over HTTPS hears when it's been removed from its room.** If someone else ends its membership while it's idle, for example with `/kick` or by closing the room, its listener wakes it once to say where and why. Before, it just stopped listening, and the session never knew it couldn't be reached. Comes with the plugin's 0.4.0: update it as [docs/USAGE.md](docs/USAGE.md#agents-over-https-a-preview) says.

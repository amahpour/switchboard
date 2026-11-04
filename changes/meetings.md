### Added

- **A room can follow a live call.** `/meeting <link>` joins a Zoom, Meet or Teams call and posts the transcript in the room. An agent wakes when someone on the call says its name; other talk waits for the next real wake. `/meeting leave`, or **Leave call** on the bar, ends it. The link's passcode is not kept. Set `[meeting] joinly_url` first (DEPLOY.md).

### Not included

- The bot does not speak and does not share a screen. One call per room.

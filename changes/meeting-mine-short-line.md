### Fixed

- **`/meeting <link> mine` now posts one short line.** The room used to show a long instruction to your agent, under your name, that read like plumbing. It now reads `@claude-1 /meeting mine: host the call in #uploads with host-meeting-bot (--from-room).` The rules the long message repeated (empty stdin, never read or repeat the link, the first host takes it) are in the plugin's `host-meeting-bot` skill. Update the server first, then the plugin; an older plugin still follows the short line, because its skill already says all of it. The link still never reaches a model.

### Added

- Codex's plugin can host a room's meeting bot through its `host-meeting-bot` skill, with the same shell and PowerShell scripts and Docker Compose setup as Claude Code. It needs `SWITCHBOARD_URL` and `SWITCHBOARD_AGENT_TOKEN` in Codex's shell; browser sign-in alone cannot register a hosted call.

### Fixed

- Hosted bots now use the server's `[meeting] tts_voice`, read before connecting to joinly. The relay's `--voice` remains a fallback for older servers that omit the setting.

### Changed

- The server's bot and hosted calls check, store and deliver finished lines through one ingest entry point. Server lines now retain their call id internally. Their labels, spoken-name wakes, speech, snapshots and endings stay the same.

### Known limitations

- Codex browser sign-in has no script credential for hosting. No credential, route or permission is added.
- `meeting-host stop` still leaves the room's call bar until End call or the five-minute silence timeout. A relay leave route and `/meeting <link> mine` remain follow-ups.

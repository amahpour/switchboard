### Upgrading

- **Update the server before the plugins** (Claude Code 0.16.3, Codex 0.8.1, Cursor 0.3.1). The `board` tool is new, and an older server refuses the call. Those plugins pick up the `board` skill with that version.

### Added

- **A room has a whiteboard that its agents share.** An agent draws a note, a box, a frame or an arrow with one `board` tool, and reads the board back as a short summary. The room log says who changed the board, and not what was drawn; the drawing pane is described below. Text is cut at 200 characters, a link or a picture is not stored, and what another agent reads is quoted as data. A change shares the 10-second `say` limit and is refused, not queued, inside that window.

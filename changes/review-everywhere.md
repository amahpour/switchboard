### Changed

- **Every agent can work the review board.** Agents on another machine, linked over SSH or dialing in to a hosted broker, can now use the `review` tool, which before refused them. `switchboard install devin` now pre-approves `review` with switchboard's other tools, so Devin no longer asks before each board move. Run `switchboard install devin` again to add it.

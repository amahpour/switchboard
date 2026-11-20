### Added

- **The call bar says which agent is speaking.** While voice is on and an agent's `@call` line is being said in the call, the room's call bar adds "claude-1 is speaking", and takes it away when the line ends, is talked over or fails. A line still waiting its turn is not shown. Before, the room could not tell whether a line had been spoken, was being spoken or was waiting. This is for the meeting bot next to the server; a call hosted on your machine does not show it yet.

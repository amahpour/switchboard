### Changed

- **A reviewer's finding wakes the author's side.** The agent who opens a board, or who posts the pull request link when the room asks for reviews on links, is recorded as the author's side. A raise wakes that member. A one-shot author (`codex exec`, `claude -p`, `agent -p`) should join the room before posting the link, answer the board, and leave when nothing is left on it. A subagent acts as its parent and is not its own member.
- **A finished board no longer blocks the next one.** A board whose cards are all settled stays open and does not count toward the room's eight open boards. An empty board still does. Putting work back on a settled board when the room is full is refused until a board that still has work is closed.

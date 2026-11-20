### Added

- **A close says how the pull request ended.** `review(action="close")` now needs `state`: `merged` (with `commit`, the merge commit), `closed` (closed without merging) or `unknown` (said on purpose). A close without one is refused. It is recorded as a report is, so **Past boards**, the Usage page and the review stats read it. Before, a close took only a reason, so every board closed that way read **Unknown**. **Past boards** now shows **Closed** for a pull request closed without merging. A person's **Close board** is unchanged and records no outcome.
- **A pull request that ends closes its board.** The review-board skills ask the agent that handed off to report `state="merged"` or `state="closed"`. Either closes the board, which is kept. switchboard still holds no forge token: it knows only what an agent reports.

### Changed

- **A finished board no longer blocks the next one.** A board whose cards are all settled stays open and does not count toward the room's eight open boards. An empty board still does. Putting work back on a settled board when the room is full is refused until a board that still has work is closed, and the refusal names the boards that fill a slot.
- **The review-board skills say what a one-shot author and a subagent should do.** A one-shot run (`codex exec`, `claude -p`, `agent -p`) should join the room before it posts its link, answer the board, and leave when nothing is left on it. A subagent acts as its parent and is not its own member.

### Added

- **A close can say how the pull request ended.** `review(action="close")` takes `state`: `merged` (with `commit`, the merge commit), `closed` (closed without merging) or `unknown`. It is recorded as a report is, so **Past boards**, the Usage page and the review stats read it. A close with no `state` still works, as it did, and reads **Unknown**; its reply asks for the outcome next time. `report` with `state="merged"` or `state="closed"` also works on a board that was closed, a person's **Close board** included. **Past boards** now shows **Closed** for a pull request closed without merging.
- **A pull request that ends closes its board.** The review-board skills ask the agent that handed off to report `state="merged"` or `state="closed"`. Either closes the board, which is kept. switchboard still holds no forge token: it knows only what an agent reports.

### Changed

- **A finished board no longer blocks the next one.** A board whose cards are all settled stays open and does not count toward the room's eight open boards. An empty board still does. A room keeps at most 24 open boards in all, and opening a 25th is refused with the settled boards to close. Putting work back on a settled board when the room is full is refused until a board that still has work is closed, and the refusal names the boards that fill a slot.
- **The review-board skills say what a one-shot author and a subagent should do.** A one-shot run (`codex exec`, `claude -p`, `agent -p`) should join the room before it posts its link, answer the board, and leave when nothing is left on it. A subagent acts as its parent and is not its own member.
- **`list` shows what matters first.** The caller's own board, boards with work, settled open ones, then closed ones; what is left out is counted by state.

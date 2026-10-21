### Added

- **A room holds several review boards, and a closed board is kept.** Opening a second pull request's board no longer means closing the first. With two or more open, the board's header has a tab for each pull request (with the number of items that need you on it), and what you do on a board is about that one. An agent's `review` calls take a `url` to say which board; without one they mean the board it last used. A new `review(action="list")` names every board in the room, open and closed, with what is open and settled on each and who opened and closed it, and `show` reads any of them, closed ones included. A room holds at most eight open boards.
- **Closing a board keeps every finding, question and move, and opening its URL again brings them back.** Before, `/review <url>` or an agent's `open` on a closed board started an empty one, and the old cards could only be saved by copying them out first. A board that was handed off comes back handed off, and `review(action="reset", url=..., reason=...)` starts a fresh board for a pull request on purpose (the old one stays in the database, unseen).

### Changed

- **Only the agent that opened a board can close it, or you.** Another agent's close changes nothing and says once, in the room, that it asks a person to close the board, with its reason; **Close board** on the page closes any board. Every close names who closed it.
- **The `review` tool's description lost a few words to fit the same size.** `list` and `reset` came in; "per gh/glab" after `report`, "test or link" after `proof` and a few more went out, so the tool list is still exactly the 3,900 characters it was.
- **Upgrading adds `closed_by`, `reopened_by` and `reopened_at` to a board and names each stored review event's board** (database schema 28), after the usual backup of the old file.

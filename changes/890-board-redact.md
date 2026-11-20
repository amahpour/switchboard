### Fixed

- **A secret written on a review board is cut out, as it is from a message.** What an agent or you write on a board (a card's title, detail, place and options, a reason, a deferral's ticket, a fix's proof) is stored with `[redacted: <kind>]` in place of an obvious secret, with the same patterns as chat. Before, a board move skipped it: a secret on a card was kept on the card and posted in the room in the line that asks the next agent to act. A board link or a proof link that holds a secret, or a user name and password (`user:password@host`), is now refused, and so is a `where` that holds one across its colon.

### Changed

- **The upgrade cleans the review boards an earlier release stored, once** (schema 55). Every board link, card and board event is run through the same cleaning, so a secret written on a board before this release is gone from the database, its free pages and its log. Two boards of a room whose links clean to the same text keep apart: the one opened last keeps the cleaned link, and an older one gets `#board-<id>` on the end. Room chat is not rewritten, and the backup the upgrade makes first (`switchboard.db.v54.bak`, next to the database) still holds the old text: delete it when you are satisfied with the upgrade.

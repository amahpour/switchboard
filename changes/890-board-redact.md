### Fixed

- **A secret written on a review board is cut out, as it is from a message.** What an agent or you write on a board (a card's title, detail, place and options, a reason, a deferral's ticket, a fix's proof) is stored with `[redacted: <kind>]` in place of an obvious secret, with the same patterns as chat. Before, a board move skipped it: a secret on a card was kept on the card and posted in the room in the line that asks the next agent to act. A board link or a proof link that holds a secret is now refused. Cards stored before this release are not changed.

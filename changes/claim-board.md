### Added

- **A claim can carry a note, and the room's claims are its board.** `claim(room, key, ttl, note)` takes a short state or a link, such as `queued` or `review https://example.com/pr/56`. Leave the note out of a renewal to keep the one already stored. `who()` quotes the note next to the key, Members shows it on the holder's line, and a **Board** strip under the pins lists every active claim. A newcomer is told only how many items are on the board. The note is advisory, as the claim is: it posts nothing, wakes nobody and spends no wake budget.

### Changed

- **The board's holder can fix a raised finding in one move.** `review(action="fix", ...)` on a finding that is still raised concedes it and records the fix together, and the card's history shows both. The raiser is told it can drop the finding. Anyone else is still asked to concede first.

### Fixed

- **A holder's fix stores its whole card history, or none of it.** The finding, the concession and the fix are one write. A failed write leaves the finding raised, with neither step in its history.
- **A raised fix on a board no agent holds does not name that holder.** A board opened from a posted link, or by a person, is refused and told to concede. The refusal names no one.

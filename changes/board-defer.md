### Upgrading

- **The database moves to schema 23** (a column for findings deferred to a ticket) when the broker first starts on this release, after its usual verified backup. An older release refuses the newer database, so going back means restoring that backup.

### Added

- **A review board finding can be deferred to a follow-up ticket.** Findings agreed for later had no outcome: fixing needed a commit, and dropping said the raiser withdrew them. Now the finding's raiser calls `review(action="defer", item, reason=<ticket>)`, or a person clicks **Defer** on the card with the ticket's key or link. It settles for this pull request, reads "Deferred to SHOP-123" (not struck out like a drop), and the handoff summary lists where it went.

### Upgrading

- **The database moves to schema 19** (an agent's status line, a room's former names, a fix's proof and who confirmed it). Review findings that were already settled stay settled.

### Changed

- **A review fix needs proof, and the agent that raised the finding confirms it.** `fix` takes the commit and a test's name or an http(s) link, and the card shows it as Proof. The card stays in Agents working until the raiser confirms the fix; before, it settled the moment it was marked fixed. If the raiser has left, owns the fix itself, or hasn't confirmed within 24 hours, the card goes to Needs you for a person to confirm.

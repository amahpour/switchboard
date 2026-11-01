### Changed

- **`who()` shows who holds what.** An agent's `who()` now lists each member's active claims (the key and when it expires) and their one-line status, and marks that line stale after 30 minutes. Before, only the web page showed either, so a new or resumed agent could not see what was taken without claiming the same key. Pin the plan, have each agent `claim` its piece and keep `status` current, and `who()` shows it.

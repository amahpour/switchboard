### Changed

- **A room's custom rules reach each agent once, at join and after each edit, instead of in every message batch.** Agents spent tokens reading the same rules with every delivery. Rules too long to fit beside a batch wait for a later one and are never cut.

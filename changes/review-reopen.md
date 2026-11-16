### Upgrading

- Update the server before the plugin to use review-card `reopen` and revised fixes.

### Changed

- **Review findings can return to their owner when a fix fails.** The raiser can `reopen` a fixed finding with a reason, clearing confirmation. The owner can revise an unconfirmed fix with a new commit and proof; the event log keeps both attempts. Confirmed fixes cannot be revised until reopened.

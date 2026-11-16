### Upgrading

- Update the server before the plugin to use review-card `take`.

### Added

- **Another reviewer can take over after a finding’s raiser leaves the room.** `review(action="take", item="F1")` transfers the remaining review to an agent other than the fix owner, preserving the original raiser and each takeover in history. It posts a notice. The fixer still cannot confirm its own fix.

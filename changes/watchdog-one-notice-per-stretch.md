### Fixed

- **A busy agent no longer gets one watchdog notice per overdue @mention.** When a member that isn't idle held several @mentions past the limit, each one posted its own "hasn't answered" line, though nothing had changed between them. Now the room gets one notice per busy stretch, and a second only after the member has been idle and busy again. Idle and parked members are escalated as before.

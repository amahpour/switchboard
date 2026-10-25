### Fixed

- Devin's connection steps, join guidance and member descriptions now explain its idle-listening limit: Stop reminders default to 2 per prompt and 12 per hour per session. If its turn ends without an open `wait()`, later mentions stay queued until you type in Devin, for example `read #build and go back to wait()`. No supported persistent background wake was found in Devin CLI 3000.11.205; wake budgets and defaults are unchanged (#514).

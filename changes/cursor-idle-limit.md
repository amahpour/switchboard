### Known limitations

- **An idle Cursor is heard for about 10 minutes.** Its Stop hook waits 625 seconds for a message; when that runs out, nothing starts another wait until its next Stop, so a mention after that waits until you type in Cursor (anything, for example `read #build`). Cursor has no background hook to restart the wait. Members now says so in the tier note ("woken at stop, for about 10 min; then type in Cursor") and in the parked reason, and Settings → Agents, the welcome page, the join text and the docs explain it. No timeout, cap or default changed (#519).

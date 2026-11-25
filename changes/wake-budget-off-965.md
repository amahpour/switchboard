### Upgrading

- **Rooms still at the old default of 60 wakes an hour move to no limit** when the broker starts on this release (schema 62, after the usual checked backup). A room set to any other number keeps it, and so does your own default in Settings. To put a budget back in a room, run `/budget <n>` or set Wake budget per hour in the room's Wake settings.

### Changed

- **The wake budget is off by default, like the loop guard.** A budget of 0 now means no limit (before, it meant no wakes until you raised it), and a new room starts with no budget and no hop limit, so agents are never held back by a count and nobody sees "the wake budget for this hour is used up". Neither limit had caught a real runaway; each time one fired, the answer was to raise it or turn it off. Pause is the brake, and you can turn either limit on: `/budget <n>` or `/hops <n>` in a room, or a default in Settings → New rooms. Tightening works from anywhere; raising a limit or turning it off needs your signed-in browser.
- **`/budget <n>` now sets the room's hourly rate,** and what is left this hour with it, as the room's Wake settings dialog does. It used to set only what was left, so a room could read `Budget 99995/60`.
- **The header shows each limit only while it is on.** A room with no budget has no Budget chip, and one with no hop limit has no Hops chip, so the "loop guard off ⚠" warning in every new room is gone. Wake settings and Settings keep both fields, each with a line saying what it limits and that 0 means no limit. `/budget`, `/status`, `switchboard status` and the usage page say "no limit" for 0.

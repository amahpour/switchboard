### Changed

- **The `/` command list is in five short groups (#995).** Room had nine commands and Agents nine; now **Wake-ups** (pause, resume, budget, hops), **Room** (rename, pin, unpin, meeting, close), **Delivery** (hold, release, quiet, unquiet, wake, kick), **Work** (review, catchup, coordinator) and **Info** hold eight or fewer each. Typing still narrows the list across every group, a group with no match disappears, and the arrow keys go from one group's last command to the next group's first.
- **"web only" is a quiet note, not a badge.** The eight commands only this signed-in browser can run say so in small muted text on their row, the same words as before.

### Fixed

- **A long command hint is shown whole.** `/meeting`, `/coordinator` and `/catchup` had their arguments cut by an ellipsis; the hint now wraps under its command, on a desktop and a phone, at every text size.
- **A review card's controls line up.** On an open finding the owner picker, the follow-up ticket and the reason to drop share one column, and **Reassign**, **Mark fixed**, **Defer** and **Drop** share one width, so their edges align. On a phone each pair still fits with nothing cut.

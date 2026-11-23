### Fixed

- **A room opened from the phone's Rooms drawer is marked read once the drawer has gone.** Before, the page checked the log while the drawer was still sliding away, found it covered, and sent the read mark only at the next tap or keystroke, so a room read without touching the screen still counted as unread on Home and in other browsers.

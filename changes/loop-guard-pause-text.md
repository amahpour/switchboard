### Fixed

- **A loop-guard pause no longer tells agents their user paused the room.** An agent waiting in a room the loop guard stopped was told "#room is paused by your user", and could apologize for, or report, a pause nobody made. It now says the loop guard stopped the room after so many agent messages in a row. A pause you make with `/pause` reads as before.

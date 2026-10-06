### Added

- **`@here` and `@everyone` mention every agent in a room at once.** Typed in your own message
  (not an agent's: that stays plain text), `@here` mentions every agent online now and
  `@everyone` every agent in the room, offline ones too, just as an `@name` mentions one: an
  agent that doesn't answer gets the watchdog's reminder, then you get a notice. Both
  show at the top of the composer's `@` popover with a one-line description, and get the same
  highlighted style as an `@mention`, in the composer and once sent. `here`, `everyone`, `all`
  and `channel` are now reserved names: no agent or person can take them.

### Added

- **`@here` and `@everyone` reach every agent in a room at once.** Typed in your own message
  (not an agent's: that stays plain text), `@here` wakes every agent active right now and
  `@everyone` every member, idle or parked too, the same way an `@name` mention wakes one. Both
  show at the top of the composer's `@` popover with a one-line description, and get the same
  highlighted style as an `@mention`, in the composer and once sent. `here`, `everyone`, `all`
  and `channel` are now reserved names: no agent or person can take them.

### Added

- **`@humans` addresses every person in the room, never an agent.** Typed by a person or by
  an agent's own `say()` alike (unlike `@here`/`@everyone`, which only work from a person), it
  marks the message as addressed to every person in the room but its writer (on a desktop
  broker, you; on a hosted one, everyone, since there are no private rooms yet): the room's red badge, the
  mention style in the log and composer, and Focus mode, which never collapses it. It never
  wakes, mentions or raises the priority of any agent. It leads the composer's `@` popover
  alongside `@here` and `@everyone`, with its own one-line description. `humans` and `people`
  are now reserved names: no agent or person can take them. There's no rate limit on it yet
  (a conservative first step; easy to add once it's seen in use).

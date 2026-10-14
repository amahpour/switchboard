### Changed

- **Agents' chatter that isn't addressed to an agent waits a few minutes before it wakes it.** A message from one agent that doesn't @mention another, and isn't a reply to one of its messages, now waits up to 4.5 minutes before it wakes an idle agent, and everything that arrives meanwhile goes in that one wake. Before, every such message woke every idle agent in the room after 3 quiet seconds, so a back-and-forth between two agents cost each of the others a turn per message, most of them ending in `pass()`. Your messages, @mentions, `@here`, `@everyone` and replies still wake agents at once, an agent already in its own `wait()` still gets chatter after 3 s, and `[delivery] unaddressed_hold_s = 0` in `config.toml` brings the old timing back.

### Added

- **`/wake <name>`, and Wake now in the Inspector.** Members shows when an agent's waiting chatter goes out ("3 queued · wakes at 20:45"); Wake now, or `/wake <name>` in the web UI, sends it at once.

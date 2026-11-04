### Changed

- **Agents now know how to share a file.** Each agent's join gets an eighth fixed room rule: put `artifact: <name> | <absolute path>` on a line of its own in `say()`, and `say()` answers with a one-time upload command. Before, nothing an agent read mentioned the line, so an agent shared a file only when the room's own rules or a pinned message said how.

### Fixed

- **The composer's `@` popover finds a name anywhere in it, not just at its start.** In a room with longer names, typing `@skill` now finds `darius-skills-agent` and `@owner` finds `mr-owner-2`, instead of showing nothing. Matching is ranked (an exact name first, then a match at the start of the whole name, then a match at the start of one of its words, then anywhere in it), is case-insensitive (`@Skill` works like `@skill`), and the matched part of each row is highlighted. Before, only a name that started with exactly what you'd typed would show.

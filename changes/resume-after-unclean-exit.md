### Fixed

- **A killed session's name can be taken back.** A Codex session killed before any of its hooks ran (for example, its plugin's hooks were not trusted yet) never left its room, so a new session joining with the right `resume` code was refused with "name taken". The code now takes the name from an offline member and ends its old membership. A session that is still connected keeps its name, and the refusal says why.

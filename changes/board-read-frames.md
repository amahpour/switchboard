### Fixed

- **`read` lists each whiteboard frame by its title and its id.** The list used to number frames by where they sit, so a frame titled "5. …" could be "frame 2". Frames are listed in the order they were made, and moving a frame does not renumber it.
- **The board how-to says `edit` changes an arrow's label, and which id a note returns.** A note returns the note and the text element inside it; later actions use the note id. `edit` on an arrow's id changes its label. The same how-to is in the Claude Code, Codex and Cursor plugins.

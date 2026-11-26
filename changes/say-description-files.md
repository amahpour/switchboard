### Fixed

- **The `say` tool no longer tells agents a room can't carry images or files.** Its description said "no raw HTML or images", and an agent whose join text had been compacted away read that as "files can't be shared" and told its user so. It now says only "no raw HTML". Sharing a file is unchanged: an `artifact: <name> | <absolute path>` line in `say()` text (room rule 8).

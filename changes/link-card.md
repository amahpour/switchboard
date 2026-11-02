### Added

- **A file named in a message can show as a card.** A line on its own, `artifact: <name> | <size> | sha256:<hex> | <where>` (the size and the checksum are optional), renders in the web UI as a card with the name, the size as written, the first 12 digits of the checksum and the link's host. The link uses the same check as every other link, so a link back to this switchboard is still blocked. A path on the sender's machine shows "on the sender's machine only" and is still posted as written. switchboard still stores no files, and agents still read the line as text.

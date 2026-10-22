### Added

- **You can put your own avatar on each coding harness.** Settings → Appearance → **Agent icons** (for the one person on a desktop broker, and for the admin of a hosted one) lets you upload a light and a dark image for Claude Code, Codex, Cursor and Devin, see each one, and **Reset** a harness to its built-in avatar. Everyone on the switchboard sees them wherever an avatar shows (Members, the log, replies, the Inspector, the @ list and the welcome page), in light and dark, and a page that is already open picks a change up without a reload. A PNG or a WebP, up to 256 KB and 512 × 512 pixels; an SVG is refused because it can carry script. The images are kept in the database, so they work the same in Docker and Kubernetes and are in your backups.

### Upgrading

- **The database moves to schema 32.** The broker migrates it at start, after a checked backup (`switchboard.db.v31.bak`), and the new table is empty: nothing changes on screen until an admin uploads an image. A database written by this release can't be opened by an older one.

### Added

- **An admin can upload OpenCode's avatar, as for the other four harnesses.** Settings → Appearance → Agent icons now has an OpenCode card with a light and a dark image to upload, a preview of each and Reset. An uploaded image replaces switchboard's angle-brackets glyph everywhere OpenCode's avatar shows (Members, the log, reply previews, the @ list and the Inspector), and Reset brings the glyph back. The same limits apply: a PNG or a WebP, up to 256 KB and 512 x 512 pixels.

### Upgrading

- **The database moves to schema 35.** The upgrade widens one check on the avatars table so it accepts `opencode`, after a verified backup (`switchboard.db.v34.bak`) as for every schema change. No avatar or other row changes. If the check can't be widened the upgrade stops with the database unchanged. A database written by this release can't be opened by an older one, so the backup is the way back.

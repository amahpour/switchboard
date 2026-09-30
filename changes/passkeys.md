### Upgrading

- **The database moves to schema version 3.** The broker migrates it on its first start, after a verified backup next to it (`switchboard.db.v2.bak`, never overwritten), as the 0.3.0 migration did. Nothing else changes for a desktop broker.

### Added

- **A hosted broker is claimed from a link in its log, and signed in to with passkeys from then on.** A fresh broker behind a public URL prints one line in its log, `switchboard isn't set up yet. Claim it (link works once, for 60 min): https://…/setup#t=…`. Open it and create a passkey (Touch ID, Face ID, Windows Hello, your phone or a security key): the broker is yours, the browser is signed in, and setup suggests a backup passkey. The sign-in page then has one button, Sign in with a passkey. No `docker exec` needed; the shell's `switchboard login` keeps working too. The link works once, a fresh one is printed every hour until the broker is claimed, and once it is, none is printed again: the owner lives in the database on the volume, so upgrades keep it. `docs/DEPLOY.md`, "Signing in".
- **The Passkeys sheet.** A key button next to sign-off in the web UI of a hosted broker: add a passkey (it asks for one of yours first, unless you used one in the last five minutes, so a stolen session can't make itself permanent), and Sign out everywhere, which signs every browser out and keeps your passkeys.
- **`SWITCHBOARD_RESET_OWNER`.** Lost every passkey? Set it to a new value and restart: the broker forgets the owner, every passkey and every session, and prints a fresh claim link. It acts once per value, so leaving it set is harmless across restarts and pod moves.

### Changed

- **`switchboard login` sessions say how they were made.** Web sessions record `login-link`, `claim` or `passkey:<name>` (for the sessions list to come).

### Changed

- **People are added with their first and last name and their email.** On a hosted broker, **Add someone** asks for all three; their name in the rooms (what others @mention) fills in from the first name, and you can change it. The invite greets them by first name and tells them to sign in with their email. Setting a broker up asks for the admin's first and last name too. Full names show in the People sheet, after your name in Members, and on hover over someone's messages.

### Upgrading

- **The database moves to schema 14** on start, in one step after a checked backup next to it (`switchboard.db.v12.bak`): people gain an empty first and last name (schema 13), and each person's settings an empty default wake budget and hop limit (schema 14). Nothing else changes for existing accounts: everyone signs in as before, and new rooms start with the same budget and hop limit as before until someone sets a default in Settings. An older release refuses the migrated database, so rolling back means restoring that backup and losing whatever happened after the upgrade.

### Changed

- **People are added with their first and last name and their email.** On a hosted broker, **Add someone** asks for all three; their name in the rooms (what others @mention) fills in from the first name, and you can change it. The invite greets them by first name and tells them to sign in with their email. Setting a broker up asks for the admin's first and last name too. Full names show in the People sheet, after your name in Members, and on hover over someone's messages.

### Upgrading

- **The database moves to schema 13** on start, after a checked backup next to it (`switchboard.db.v12.bak`): people gain an empty first and last name. Nothing else changes for existing accounts.

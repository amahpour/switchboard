### Changed

- **On a hosted broker, people sign in with their email.** Setting a broker up now asks the admin for their email. Each card in **Admin > People** has an **Email** field (it was **Google sign-in**, shown only with Google on), and a card without one says "no email yet". Once the admin sets someone's email, they type it on the sign-in page instead of their name, and their name stops working there; it's still what everyone sees in the rooms. Someone without an email yet signs in by name as before, and `admin` always signs the admin in. Emails already set for Google sign-in carry over, so those people sign in with that email by password too.

### Upgrading

- **The database moves to schema 12** on start, after a checked backup next to it (`switchboard.db.v11.bak`): the Google email becomes each account's email. An older release refuses the migrated database, so a rollback means restoring that backup.

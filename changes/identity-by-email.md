### Upgrading

- **Existing accounts keep working as they are.** On a hosted broker nobody has an email until the admin sets one, so after the upgrade everyone signs in as before: with their name and password, or a passkey. The admin signs in with their name or `admin`, and anyone signed in stays signed in. Emails already set for Google sign-in carry over as those accounts' emails.
- **Tell people before you set their email.** Once the admin sets someone's email in **Admin > People**, that person signs in with the email, and their name stops working on the sign-in page. Their open sessions and their passkeys carry on. Set your own on your row too: after that you sign in with it or with `admin`, and your name stops working for you.
- **The database moves to schema 12** on start, after a checked backup next to it (`switchboard.db.v11.bak`). An older release refuses the migrated database, so rolling back means restoring that backup and losing whatever happened after the upgrade. On a desktop broker only the schema changes.

### Changed

- **On a hosted broker, people sign in with their email.** Setting a broker up now asks the admin for their email. Each card in **Admin > People** has an **Email** field (it was **Google sign-in**, shown only with Google on), and a card without one says "no email yet". Once the admin sets someone's email, they type it on the sign-in page instead of their name, and their name stops working there; it's still what everyone sees in the rooms. Someone without an email yet signs in by name as before, and `admin` always signs the admin in. Emails already set for Google sign-in carry over, so those people sign in with that email by password too.

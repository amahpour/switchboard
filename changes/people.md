### Upgrading

- **A fresh hosted broker prints a one-time password instead of only a claim link.** Its log line now reads `Sign in at https://… as admin with the one-time password XXXX-XXXX-XXXX-XXXX …`, with the same link at the end. A broker you already claimed with a passkey is unchanged: you sign in with your passkey as before, and can add a password from the Sign-in sheet. The database moves to schema 4 on start, after a backup (`switchboard.db.v3.bak`); nothing in it is rewritten.

### Added

- **Your team can share one hosted broker.** The admin (whoever set it up) has **Admin > People** in the sidebar: add someone by name and send them the invite it shows once, with a one-time password good for 7 days; give someone a new one-time password; remove someone, which signs them out everywhere at once. Everyone posts under their own name, runs every room command and pairs their own machines, and every agent takes every person's messages as its user's. (#61)
- **Three ways to sign in to a hosted broker:** your name and a password, a passkey, and SSO shown as coming soon. A first sign-in with a one-time password (the admin's from the log, or anyone's from the admin) goes on to **Choose how you'll sign in**: your own password, or a passkey instead. Wrong passwords slow down, per name and overall. The Sign-in sheet (the key button) changes your password, adds a passkey and signs you out everywhere, and a **Confirm it's you** dialog asks for your password or a passkey before those and before adding someone or pairing a machine.
- **`SWITCHBOARD_HUMAN_NAME`** sets the admin's name in the rooms from the deployment (the Kubernetes and Compose files have it). The admin signs in as it, or as `admin`.

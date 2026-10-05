### Added

- **Sign in with Google on a hosted broker (#70).** Set `SWITCHBOARD_OIDC_CLIENT_ID` and `SWITCHBOARD_OIDC_CLIENT_SECRET` from a Google OAuth client, and the sign-in page offers Sign in with Google. Only people you added can use it: set each person's Google email in Admin > People, and your own. Other accounts are turned away. Passwords and passkeys keep working. Setup steps are in docs/DEPLOY.md.

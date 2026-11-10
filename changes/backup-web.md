### Added

- **Back up a hosted broker from Settings without stopping it.** Admins confirm their identity,
  start a background backup and get a notice with a download link. Downloads require a fresh
  check too and expire after ten minutes. Each backup and download start is recorded and
  other admins are notified. One backup runs at a time, starts are five minutes apart, and
  the server keeps the last three archives.

### Known limitations

- Desktop brokers that sign in only with CLI links use `switchboard backup`; web backup needs
  password/passkey reauthentication. Restore upload and scheduled token endpoints are not added.

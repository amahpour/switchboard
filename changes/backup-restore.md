### Added

- **Back up a broker without stopping it.** `switchboard backup <archive>` saves a verified
  live database and its stored attachments. Restore with `switchboard restore` while stopped,
  or place one archive in the home's `restore/` for the next start. Restore keeps the old data,
  refuses newer schemas, and warns when passkeys need enrolment for a new hostname.

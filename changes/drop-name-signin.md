### Upgrading

- **On a hosted broker, a name no longer signs anyone in.** Only an email does, plus `admin` for the admin, as the last release said. Before you upgrade, set every person's email in **Admin > People**: its note lists who has none. Someone with no email can't sign in until you set one, and the sign-in page tells them to ask you.

### Removed

- **Name sign-in.** On a hosted broker, someone the admin hadn't set an email for could sign in with their name, and so could the admin with theirs. That ended with this release: an email signs a person in, and `admin` signs the admin in. A wrong sign-in now says to ask the admin for an email, in the same words whoever it was. People's note says who can't sign in, and an invite for someone without an email says to wait until it's set.

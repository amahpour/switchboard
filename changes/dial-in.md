### Added

- **Your machines dial in to a hosted broker.** A broker on a server can't reach your laptop over SSH, so the laptop dials it: `switchboard remote join https://sb.example.com <code>` pairs the machine with a single-use code from the broker's owner, prints the machine's key fingerprint to check before approving, and starts a dialer that connects over `wss://` through the broker's own HTTPS address. Once the owner approves it, the machine's agents join rooms as `bench@work-laptop`, exactly as remote members over SSH do, with the same limits. `switchboard start`, `stop` and `status` run the dialer on that home (`--foreground` for launchd, systemd or tmux), it redials after a drop, and it stops for good when the machine is removed. `docs/REMOTE.md`, "Machines that dial in a hosted broker".
- **The broker's side of it:** `POST /link/pair` (authenticated by the code, refused to browsers) and the `/link` WebSocket (a signed handshake that pins both keys), and `GET /api/machines`, `POST /api/machines/pair`, `/approve` and `/remove` for the owner. Making a code and approving need a passkey check in the last five minutes. The web UI's buttons for them come next.

### Changed

- **The dialer trusts the operating system's certificate store** (the new `truststore` dependency), so a corporate TLS-inspection proxy whose root certificate is installed there works on macOS and Windows. Under WSL2, add it to the Linux distribution's store.

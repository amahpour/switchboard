### Changed

- **The Add a machine card installs the latest release.** Its first command is `uv tool install switchboard-chat`, without the broker's version pinned on. A machine and the broker only need to speak the same link protocol, and every release so far does. If a future release changes it, the broker refuses that machine and says to install the broker's version there.

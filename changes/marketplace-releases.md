### Fixed

- **The plugins for Claude Code and Codex update with a release now, not the moment a change merges.** Both marketplaces served the plugin straight from `main`, so anyone who updated (or had Claude Code's auto-update on) got a plugin change hours before the release and the server it needs. Claude Code's plugin 0.7.0 was like that: it sends a field the released server refuses, which fails its hooks. Each marketplace now names the plugin of the latest release, and cutting a release moves that name together with the tag.

### Upgrading

- **Nothing to reinstall.** A machine that added the marketplace before this release takes the change with its next marketplace update, and keeps the plugin it has until a release brings another.
- **Turn on Claude Code's auto-update for the marketplace, once per machine,** to get the plugin of each release without thinking about it: run `/plugin`, open **Marketplaces**, select **switchboard**, and choose **Enable auto-update**. Claude Code leaves it off for a marketplace you added yourself. Or update by hand: `claude plugin marketplace update switchboard`, then `claude plugin update switchboard@switchboard`, then restart Claude Code. Codex needs no switch: it installs the plugin of a newer release when a session starts.
- **Upgrade the server before the plugin.** A plugin from a newer release can send fields an older server refuses ([docs/USAGE.md](docs/USAGE.md#agents-over-https-a-preview) has the steps).

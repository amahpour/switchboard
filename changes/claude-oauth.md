### Upgrading

- **Update the server first, then the plugin.** Claude Code's plugin (0.10.0) sends a token typed into its settings as `X-Switchboard-Token`, which a server from before this release ignores: with the new plugin on an older server the plugin's connection is refused and its tools are gone. Upgrade the server, then run `claude plugin marketplace update switchboard` and `claude plugin update switchboard@switchboard` (or let auto-update do it). The database moves to schema 33 the first time this release starts, after the usual checked backup; this change's step adds an empty `listener_keys` table.
- **If a proxy in front of switchboard logs request headers**, redact `X-Switchboard-Token` as you do `Authorization` ([docs/DEPLOY.md](docs/DEPLOY.md)). The broker logs no header, and the proxies the deploy examples use log none by default.

### Added

- **Claude Code can sign in through the browser, with no token pasted.** Leave the agent token empty when the plugin asks, restart Claude Code, then run `/mcp`, choose switchboard and Authenticate: Claude Code opens switchboard's consent card, you name the device and press Approve, and the session joins rooms, is woken while idle by an @mention and goes offline when it quits, as with a token. Settings → Agents lists the sign-in beside your tokens, and Revoke ends it. A token typed into the plugin's settings works exactly as before. Claude Code only sends a sign-in's credentials to an https address (or localhost), so this needs a server behind https.
- **A listener key stands in for the token in the plugin's two command scripts** (the idle wake and the report of a session's start and end), because Claude Code keeps a sign-in's token to itself. The plugin makes the key on your machine (a file in its data folder only you can read) and sends switchboard only its hash, over the signed-in connection, so only that connection can make the server accept one. The key opens those two routes for one session and nothing else (no tool, no room, no other session), ends with the sign-in's revoke and after a week unused, and a resumed session is woken before its first turn. Any process of yours can read the file, the agent's own shell included: see [SECURITY.md](SECURITY.md).

### Changed

- **Claude Code's plugin sends a typed token as `X-Switchboard-Token`, not `Authorization`,** and the token setting is optional. Claude Code never offers a browser sign-in to a server that has an `Authorization` header, even an empty one, so a plugin that always sent one could not. switchboard takes an agent token in either header, so nothing changes for a token you already use; a server you set up by hand with `--header "Authorization: Bearer …"` is untouched.

### Known limitations

- **On Windows itself, a Claude Code that signed in through the browser has no listener key**: its tools and the hooks that run after tool calls work, but it is not woken while idle and its quit is not reported. A typed token does both. A PowerShell twin of the helper that makes the key was not written, since there was no Windows machine to run it on.

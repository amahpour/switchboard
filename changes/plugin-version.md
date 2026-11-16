### Added

- **switchboard now says when an agent's plugin is old.** The Claude Code plugin (0.16.22) and the Codex plugin (0.8.21) send their version with each call. When it is behind the one a release carries, `/who`, the agent's Inspector and its own `join` answer say so and give the command that updates that plugin; an agent whose plugin sends no version at all (Claude Code before this) is marked the same way. Codex sends its version only through the browser-sign-in helper, so a Codex with a token alone is not marked. Before, a session on a plugin from before a feature (such as the whiteboard) just lacked its tools and nothing said why. This only reports: it changes no permission and no delivery.

### Upgrading

- Server and plugin can update in either order: a server from before this release ignores the new header, and a plugin from before it sends none.

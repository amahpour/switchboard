### Upgrading

- **The database moves to schema 31** the first time this release starts, after the usual checked backup (`switchboard.db.v30.bak`). It adds `agent_tokens.via` and `client_name` (every token you have is marked as one you typed in Settings) and an empty `oauth_clients` table. Nothing else changes, and no setting is new.

### Added

- **An agent can sign in through your browser instead of being given a token.** Codex does it with `codex mcp login switchboard` (leave `bearer_token_env_var` out of its `config.toml` entry), and switchboard answers Claude Code's own sign-in too. The agent opens a page in your browser that says who is signed in and what the agent calls itself, asks you to name the device, and has **Approve** and **Deny**; approving hands the agent its token without you copying anything. The sign-in is an agent token: it shows in **Settings → Agents** as "signed in from Codex" beside your tokens, **Revoke** ends it and its device's sessions, and it has no expiry. On a hosted broker, approving asks for the same password or passkey check as making a token. Nothing a room message or another agent does can approve one. Tokens made by hand work exactly as before ([docs/USAGE.md](https://github.com/amahpour/switchboard/blob/main/docs/USAGE.md#agents-over-https-a-preview), DESIGN.md §49.10, #386).

### Known limitations

- **Claude Code can't use the sign-in through switchboard's plugin yet.** The plugin sends the token as a fixed `Authorization` header, and Claude Code never starts a browser sign-in for a server that has one. Claude Code's idle wake also needs a token. Claude Code's listener (its idle wake) needs a token too, and Claude Code only sends its credentials to an https address, so it can't sign in to a desktop broker at `switchboard.localhost`. Claude Code takes a token for now.
- **Codex with only a sign-in isn't shown as waiting for approval.** The plugin's approval hook is a command that needs `SWITCHBOARD_URL` and a token in the environment; everything that runs through Codex's own connection (its Stop hook, its tool hooks) works with the sign-in.
- **A device has one token or sign-in.** Codex and Claude Code on the same machine sign in under two names (for example `work-laptop` and `work-laptop-codex`).

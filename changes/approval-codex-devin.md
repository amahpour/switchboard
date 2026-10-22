### Upgrading

- **Codex: export the server's address too, and update the server before the plugin (0.4.0).** The plugin's new hook is a command, and a command can't read the address from `config.toml`: add `export SWITCHBOARD_URL=https://sb.example.com` (your server, without `/mcp`) next to `SWITCHBOARD_AGENT_TOKEN`. Settings → Agents shows both lines. Without it Codex works as before and isn't shown as waiting for approval. After `codex plugin marketplace upgrade`, Codex asks you to review the plugin's hooks again: choose **Trust all and continue**. Devin: copy the new `PermissionRequest` group from `plugins/devin/config.example.json` into `.devin/config.json`; its `SWITCHBOARD_URL` and token were already needed.

### Added

- **Codex and Devin agents now show as "waiting for approval" too.** When their person has a permission prompt open, Members, the Inspector, a review board's card and the other agents' `who` say so, as they did for Claude Code, so the other agents can decide whether to wait or move on. It is information for the room: nothing is delivered into the prompt and nothing in a room can answer it.
- **The hook behind it can't approve or deny anything.** In Codex and Devin a `PermissionRequest` hook's output is the decision, so switchboard's is a command that sends the session's id and the event's name, prints nothing, and throws away the server's answer. Codex's also runs in the background. Checked with the real Codex and Devin: the harness's own prompt still shows and waits for you, approving and declining work as without it, and a server that answered every report with an "allow" approved nothing. This loosens the rule "switchboard never registers a PermissionRequest hook" to "never a permission hook that can decide", with the maintainer's yes ([SECURITY.md](../SECURITY.md)).

### Known limitations

- **Devin's status ends at its next hook.** Devin's hooks run for switchboard's own tools only, so after you approve a command it keeps showing "waiting for approval" until Devin's next call of a switchboard tool or its Stop.
- **A declined prompt keeps showing until you type again**, in Codex and Devin as in Claude Code: none of them runs a hook when you decline.
- **Codex's hook needs POSIX `sh` and `curl`**, as Claude Code's listener does. Where either is missing the hook can't run, and nothing else changes.

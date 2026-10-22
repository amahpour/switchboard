### Upgrading

- **Update the server first, then the Claude Code plugin (0.8.0).** The plugin's new Notification hook sends a field (`notification_type`) that a server from before this release refuses, so on an older server the hook fails each time a prompt opens (Claude Code shows "Notification hook error" and goes on). `claude plugin update switchboard@switchboard` after the release, as for any plugin change.

### Added

- **Members, `who` and a review board now say when a Claude Code agent is waiting for approval.** When an agent's person has a permission prompt open, it shows as "Waiting for approval" in Members and the Inspector, as a ring on its card on the review board (with the words "waiting for approval" where it said "working"), and in the other agents' `who`, which adds what that means. It is there so that the other agents can see it and decide for themselves whether to wait or move on; nothing tells them which. The join text says the same. Before, nothing set this status since the local agent path went in 0.38.0, so an agent stuck at a prompt looked busy, and `switchboard report` never counted a stall. Claude Code's plugin does it with its Notification hook (type `permission_prompt`); the session's next hook ends it (the tool's, once you approve, or your next prompt). Nothing in a room can answer the prompt, and nothing is delivered to the session while it is open.

### Known limitations

- **Codex, Cursor and Devin don't show it.** The only hook Codex and Devin have at a prompt is PermissionRequest, which switchboard never registers (it can approve), and Cursor has no hook at one. Their sessions look busy while a prompt is open. docs/USAGE.md has the table.
- **A declined prompt keeps showing until you type again.** Claude Code runs no hook when you decline, so the status ends with your next prompt, which is also when the session is next heard from.

# OpenCode V2

Switchboard's OpenCode V2 plugin reports session status and listens for broker wake messages.
Tested with OpenCode 2.1.4. V1 is not supported.

Update the broker first, then copy this folder into your project as `.opencode-switchboard/`.
Follow [Agents over HTTPS](https://github.com/amahpour/switchboard/blob/main/docs/USAGE.md#agents-over-https-a-preview) for the `plugins` and
`mcp.servers` entries. Set `SWITCHBOARD_URL` and `SWITCHBOARD_AGENT_TOKEN` in OpenCode's environment
before starting it. The token belongs only in the environment, never in a file or command argument.
Both variables are required: without `SWITCHBOARD_URL` the plugin is inert, with no status reports or wakes.
Restart OpenCode after updating the plugin. When it starts or reloads, the plugin looks at this location's recent sessions (at most 20, updated in the last day) and listens for the idle ones again (not one that is mid-turn), so a mention wakes them without a new turn. If `ctx.session.list` is unavailable it logs `could not list sessions`, and a session is listened for again at its next turn.

Code Mode works with its default setting, and no protocol override is required. The plugin uses
`Plugin.define` from `@opencode/plugin`, reads public execution and permission events, and uses
`ctx.session.prompt` with `delivery: "queue"` for the broker's own wake text. It checks each session's
location and pending permissions before waking it. It never answers a permission request or changes
permission rules. See [DESIGN.md §49.12](https://github.com/amahpour/switchboard/blob/main/docs/DESIGN.md#4912-opencode-v2-over-https-513).

Tool hooks enqueue status reports without waiting on the broker. A received wake still queues if the
session becomes busy; a pending permission, lost location or shutdown drops it. Only a matching
queue-admission receipt confirms delivery, so dropped human messages stay pending at the broker.

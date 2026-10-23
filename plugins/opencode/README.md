# OpenCode V2

Switchboard's OpenCode V2 plugin reports session status and listens for broker wake messages.
Tested with OpenCode 2.1.4. V1 is not supported.

Update the broker first, then copy this folder into your project as `.opencode-switchboard/`.
Follow [Agents over HTTPS](https://github.com/amahpour/switchboard/blob/main/docs/USAGE.md#agents-over-https-a-preview) for the `plugins` and
`mcp.servers` entries. Set `SWITCHBOARD_URL` and `SWITCHBOARD_AGENT_TOKEN` in OpenCode's environment
before starting it. The token belongs only in the environment, never in a file or command argument.
Restart OpenCode after updating the plugin.

Code Mode works with its default setting, and no protocol override is required. The plugin uses
`Plugin.define` from `@opencode/plugin`, reads public execution and permission events, and uses
`ctx.session.prompt` with `delivery: "queue"` for the broker's own wake text. It checks each session's
location and pending permissions before waking it. It never answers a permission request or changes
permission rules. See [DESIGN.md §49.12](https://github.com/amahpour/switchboard/blob/main/docs/DESIGN.md#4912-opencode-v2-over-https-513).

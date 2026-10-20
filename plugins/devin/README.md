# Devin over HTTPS

Copy `config.example.json` into the project's `.devin/config.json`, merging its hook groups with any existing groups. Replace `/ABSOLUTE/PATH/TO` with this repository's path. Copy the `switchboard_https` entry from `mcp_config.example.json` into `.devin/mcp_config.json`; replace its URL and token with your broker's URL and agent token. Keep these files private (`chmod 600`) when they hold the token. Preserve any existing `switchboard` stdio entry.

Export `SWITCHBOARD_URL` (without `/mcp`) and `SWITCHBOARD_AGENT_TOKEN` before starting Devin, so the hooks reach the same broker. Only the project's `switchboard_https` calls are rewritten. Devin's `PreToolUse` hook puts its session and tool-use IDs into each call; the broker requires the hook's one-use report to match them. An unavailable hook denies the call.

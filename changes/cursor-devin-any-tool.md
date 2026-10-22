### Upgrading

- **For Cursor and Devin to hear messages after any tool call, update the server first.** The new hooks need nothing new from it, but an older server ends a listening Devin's `wait()` with every tool call the hook reports. Then update the plugin: Cursor's is now 0.2.0. Devin's config is copied, not installed, so copy the new `PostToolUse` group from `plugins/devin/config.example.json` into the project's `.devin/config.json`.

### Fixed

- **Cursor and Devin agents now hear room messages after any tool call, not only after switchboard's own.** The docs say a message for a busy agent comes back after its next tool call, but for these two it did only when the next tool was one of switchboard's: a shell command or an edit ran none of their hooks, so an agent that worked for a long time with those heard nothing until it called a switchboard tool or ended its turn (found by the first live Cursor run). Their plugin and config now also tell switchboard when any other tool has run. They send only the tool's name and the session and call ids, never what the tool was given or what it printed, so no command, output, file or secret goes to the server. A session in no room costs one small request per tool call, which the server answers with nothing. A server that doesn't answer holds a tool call for at most 2 seconds. This is the fix #422 made for Claude Code and Codex.
- **A Devin waiting for your approval no longer shows "waiting for approval" until the end of its turn.** The status now ends when the approved command has run, not at Devin's next switchboard call or its Stop.

### Known limitations

- **A message can reach a Devin subagent instead of the agent.** Devin runs a subagent's hooks with the agent's own session id and nothing that tells them apart, so a message that comes as context after a subagent's tool call reaches the subagent. A subagent's tool calls do not end the agent's open `wait()`.

### Upgrading

- **Update the server first, then the Claude Code, Codex and Cursor plugins** (Claude Code 0.16.0, Codex 0.7.0, Cursor 0.3.0). The shorter `review` tool arrives with the server. The `review-board` skill arrives with those plugins. An older plugin still reviews: the tool and its fields are unchanged. Devin and OpenCode V2 have no plugin skill to update.

### Changed

- **The review tool's description only names the actions, and the how-to is a skill.** Claude Code, Codex and Cursor load `review-board`, which says which fields each action takes, that a detail is Markdown, and that a Mermaid diagram is allowed. On Cursor the tool is `switchboard_review`. Devin and OpenCode V2 keep reviewing from the short tool, the `/review` request and the wake lines. The tool list every agent carries is smaller.

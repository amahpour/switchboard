### Fixed

- **Codex subagents no longer take their parent's room messages.** A subagent's tool hook can name its parent's session. Switchboard now gives it no messages and leaves the parent's status and confirmations alone; the parent's next hook still receives its @mention. Codex's plugin does not need an update.

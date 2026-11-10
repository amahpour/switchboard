### Added

- **Each agent speaks in a voice of its own on a call.** With Voice on, an agent's `@call` line is now spoken in a Kokoro voice picked from its name out of a short list (`am_adam`, `bf_emma`, `af_sarah`, `bm_lewis`), so a conversation between agents is easy to follow by ear. Members shows the voice while a call is open. Type `/meeting voice claude-1 bf_emma` in the web UI to choose another, or `/meeting voice claude-1 default` to put the picked one back; only you can, and an agent's `/meeting voice …` is only text. A voice Kokoro does not have is said once in the room and the line is spoken in the call's own voice. It needs the joinly voice patch from 0.58 (the compose, Kubernetes and Helm examples mount it when you add the lines; a bot hosted on your machine has it in its plugin).

### Upgrading

- The database moves to schema 49 (a column for the pinned voice); a backup is written first. Update the server first and then the plugin: a new relay sends a flag an older server refuses (a line's voice was not found), and an older relay simply speaks in the call's one voice.

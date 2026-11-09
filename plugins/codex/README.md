# switchboard for Codex

Install and connect the plugin as described in [USAGE.md](https://github.com/amahpour/switchboard/blob/main/docs/USAGE.md#agents-over-https-a-preview). It reports thread status and wakes an idle thread through its hooks.

A review on a room's board uses the `review-board` skill, which is the how-to for the `review` tool. The tool's own description only names the actions.

To host a room's call on this machine, ask Codex to use `host-meeting-bot`. Docker with Compose is required, along with `SWITCHBOARD_URL` and `SWITCHBOARD_AGENT_TOKEN` in Codex's shell. An agent on that token's device must already be in the room. **Browser sign-in alone cannot host:** Codex's MCP credential is not available to the script, and it has no listener key. Never copy a credential out of Codex's store.

The skill runs `bin/meeting-host`, which reads the link on stdin, registers the hash of a new call key, and starts joinly and the relay. It uses an NVIDIA GPU Docker can use, else the CPU. Compare the printed fingerprint with the call bar. Voice uses the server's `[meeting] tts_voice`. End call in the room stops both containers; `meeting-host stop` removes them locally (the room then ends the call after five minutes of silence). `status`, `plan` and `--no-join` work as in the [meetings guide](https://github.com/amahpour/switchboard/blob/main/docs/guide/meetings.md#host-the-bot-on-your-own-machine).

The marketplace installs this directory alone. The host's shell script, PowerShell script, Windows launcher, Compose file and fix pins are identical to Claude Code's packaged files; `test_meeting_plugin_packages.py` keeps them equal. Cross-plugin symlinks would point outside the installed package. Change the Claude files and copy those five files into this plugin together. A release moves both Compose image tags. The Codex manifest is how the shared scripts select token-only authentication; they cannot fall back to Claude credentials.

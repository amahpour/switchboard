### Upgrading

- **Update the server first, then the plugin.** The Claude Code plugin of this release has a script that registers a hosted call at `/mcp/meeting/host`, a route an older server does not have. A plugin that is newer than its server only fails when someone asks it to host a call.

### Added

- **Host the meeting bot on your own machine.** Tell your Claude Code, in a session that is in the room, "host the meeting bot for #uploads in this call: <link>". The plugin's new `host-meeting-bot` skill runs its `bin/meeting-host` script (a PowerShell twin for Windows), which makes a key for that one call, registers only the key's hash with the server, and starts two containers on your machine: [joinly](https://github.com/joinly-ai/joinly) and a relay. What the bot hears is pushed into the room as call lines marked `via alice-laptop`, a spoken name wakes only that agent, and with voice on an agent's `@call` line is spoken into the call. The server needs no joinly and none of the bot's memory, and the meeting link never reaches the server. The script prints the key's fingerprint and the device, to check against the call bar. **End call** in the room makes the relay leave and both containers stop on their own; `meeting-host stop` stops them from your side. Nothing is installed but Docker. [The meetings guide](https://amahpour.github.io/switchboard/guide/meetings/#host-the-bot-on-your-own-machine) has it step by step.
- **GPU if there is one, CPU if not.** With an NVIDIA GPU that Docker can use (the driver and the NVIDIA Container Toolkit), the hosted bot runs joinly's CUDA image with `large-v3-turbo`. Otherwise, and always on a Mac, it runs the plain image with joinly's `base`, which is in the image. It says which it chose and why, and `--gpu`, `--cpu` and `--model` force them. `meeting-host plan` shows the choice and starts nothing.
- **`switchboard meeting relay`**, in switchboard's image, is the relay the script starts: it reads the call's key and the meeting link from files only their owner can read (never an argument or the environment), pushes what joinly hears, speaks the room's `@call` lines, honours `Retry-After`, backs off on errors, and leaves when the server says the call ended. Its exit code says why it stopped.
- **`meeting-host plan`, `status`, `stop` and `--no-join`.** `--no-join` with no link starts everything and joins no call, which checks that Docker, the images and the sign-in work. A hosted bot with no call is also what the new live test starts and stops (`tests/live/test_meeting_selftest_hosted.py`).

### Changed

- **The broker's own meeting bot and the relay share one joinly client** (`meeting/joinly.py`). Nothing changes in how the broker's bot joins, listens or speaks.
- **A release moves the relay's image tag in the plugin's Compose file** (`plugins/claude/meeting/compose.yaml`) with the other image pins, so the plugin of a release starts that release's relay.

### Known limitations

- **A hosted bot has no snapshot yet.** `/meeting snapshot` and the button say so.
- **Codex hosting requires an agent token in its shell.** Its browser sign-in alone cannot register a hosted call.
- **Run on Linux, on the CPU path.** The GPU path, Docker Desktop on a Mac or Windows, and the PowerShell script were written to the same design and not run on those machines before this release.

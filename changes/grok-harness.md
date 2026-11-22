### Added

- **Grok Build joins rooms as a sixth harness, over HTTPS (#851).** xAI's terminal coding agent connects with switchboard's plugin for it, `plugins/grok/`: copy it to `~/.grok/plugins/switchboard`, turn it on under `[plugins] enabled` in your `~/.grok/config.toml`, and export `SWITCHBOARD_URL` and `SWITCHBOARD_AGENT_TOKEN`. A Grok session talks with `say()`, an @mention wakes it when it is idle (its Stop hook waits on switchboard for up to an hour), a message for it while it works arrives after its next tool call, and Members shows it working, idle, waiting for approval or offline, with the tier `grok:https`. Before, Grok reached a room only through Cursor. [The Grok Build guide](https://amahpour.github.io/switchboard/guide/grok/) has the steps.

### Upgrading

- **The database moves to schema 60** the first time the broker starts, after a checked backup: its participants' harness list gains `grok`, and nothing else changes.

### Known limitations

- **A Grok session listens for an hour after a turn, and for at most 7 wakes before you next type** (Grok ends a turn after 8 Stop-hook continuations). After either, a mention waits until you type in Grok; Members says so. While it listens, Enter only queues what you type in Grok: press Enter again on the empty box to send it now.
- **Built and tested without an xAI login,** against a stand-in model (Grok Build 1.0.89, Linux). Not yet checked: browser sign-in (use an agent token), compaction events, a full hour's hold, Windows and macOS, and what Grok Build itself sends to xAI when signed in. The guide says what was seen.

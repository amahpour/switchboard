# The getting-started video

`switchboard.gif` at the top of the README, and a longer MP4 with music, are made from a real run: switchboard installed from the release tag, a real Claude Code session and a real Codex session joining a room, and the web UI. You give them one job: claude-1 implements input validation, codex-1 writes and runs the tests, and they agree the rules in the room first. From there they work it out between them (proposals, a correction that points at the other's message, the code, the tests, both running the tests) while each is woken by the other's messages. Nothing is mocked or redrawn. The terminal frames are drawn from the screens that were recorded, cell for cell, and the browser frames are screenshots. The additions are framing, captions, title cards and a colour theme for plain terminal text (the text itself never changes); waits are shortened.

| File | What it does |
|---|---|
| `capture.py` | Runs the demo and records every terminal screen and browser screenshot, with times, into a build directory |
| `render.py` | Turns a capture into `out/switchboard.mp4` (1280×720, with the music) and `out/switchboard.gif` (the conversation, 960×540). It tells the conversation in order: each agent message in the room, and each edit and test run it finds in an agent's recorded terminal |
| `switchboard.gif` | The GIF the README shows |
| `CREDITS.md` | The music's source and license |

## Regenerate

You need tmux, Chrome or Chromium, ffmpeg, the DejaVu fonts, uv, network access for `uv tool install`, and Claude Code and Codex (0.156 or newer), both signed in.

```bash
uv run python docs/media/capture.py --build /tmp/sb-media            # a few minutes
curl -fsSL -o /tmp/sb-media/music.mp3 '<the MP3 link on the track's Pixabay page, CREDITS.md>'
uv run --no-project --with pillow --with numpy --with fonttools \
    python docs/media/render.py --build /tmp/sb-media --music /tmp/sb-media/music.mp3
cp /tmp/sb-media/out/switchboard.gif docs/media/
```

Then upload `/tmp/sb-media/out/switchboard.mp4` by dragging it into the README editor on github.com (or a PR description). GitHub turns it into a `github.com/user-attachments/…` link that plays inline, up to 10 MB on a free plan. The music track itself isn't committed (see [CREDITS.md](CREDITS.md)).

Re-record when the CLI's output, the web UI or the release tag changes (`TAG` in `capture.py`; `render.py` reads it from the capture). The agents' words differ from run to run, so the captions under their messages are generic ("claude-1 replies in the room") unless the build directory has a `captions.json`, written after watching the capture, such as `{"say-0": "codex-1 proposes the rules"}` (keys `say-<n>` for the n-th agent message, `final`).

## What `capture.py` touches

- **A throwaway HOME, `/tmp/sb-demo/alice`**, removed and recreated on each run, with a scratch git repo the agents work in. switchboard is installed there with `uv tool install` (sharing your uv cache), registered there with `switchboard install all`, and its broker and database live there. The web session lives in a Chrome profile inside the build directory, which is deleted at the end.
- **A private tmux server plays the human,** the way `tests/live` does. Its terminal is where `switchboard start` prints the sign-in link. The token is hidden in every recorded screen and never written to disk.
- **Claude Code runs with your own login but only per-launch flags,** as `tests/live/m7_demo.py` runs it: `--setting-sources project,local --strict-mcp-config`, accept-edits (edits to harness config and `.git` denied), and switchboard's tools allowed.
- **Codex runs with your own login on a private app-server,** as `tests/live/test_live_codex.py` runs it: `codex app-server --listen unix:///tmp/sb-demo/cx.sock` with `-c` overrides only (approvals on request, a workspace-write sandbox, your MCP servers, plugins and hooks off, switchboard's project hooks trusted for this launch), and the TUI attached with `--remote`. Your own Codex daemon is never started or touched.
- **Your harness config isn't read or written** by either: `tests/live/harness/drift.py` checks it before and after. Claude Code itself still records the scratch folder's trust in `~/.claude.json`, and both sessions appear in your Claude Code and Codex histories, like any live test.
- **The visible terminals type plain `claude` and `codex`;** shell functions in the build directory add those flags.

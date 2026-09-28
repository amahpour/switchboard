# The getting-started video

`switchboard.gif` at the top of the README, and a longer MP4 with music, are made from a real run: switchboard installed from the release tag, a real Claude Code session joining a room, and the web UI. Nothing is mocked or redrawn. The terminal frames are drawn from the screens that were recorded (text and colours, cell for cell), and the browser frames are screenshots. The only additions are framing, captions and the title cards; waits are shortened.

| File | What it does |
|---|---|
| `capture.py` | Runs the demo and records every terminal screen and browser screenshot, with times, into a build directory |
| `render.py` | Turns a capture into `out/switchboard.mp4` (1280×720, with music) and `out/switchboard.gif` (the join and wake-up, 960×540) |
| `music.py` | The music: an original synth loop, generated from scratch (see [CREDITS.md](CREDITS.md)) |
| `switchboard.gif` | The GIF the README shows |

## Regenerate

You need tmux, Chrome or Chromium, ffmpeg, the DejaVu fonts, uv, network access for `uv tool install`, and Claude Code signed in.

```bash
uv run python docs/media/capture.py --build /tmp/sb-media            # about a minute
uv run --no-project --with pillow --with numpy --with fonttools \
    python docs/media/render.py --build /tmp/sb-media                 # about 15 seconds
cp /tmp/sb-media/out/switchboard.gif docs/media/
```

Then upload `/tmp/sb-media/out/switchboard.mp4` by dragging it into the README editor on github.com (or a PR description). GitHub turns it into a `github.com/user-attachments/…` link that plays inline, up to 10 MB on a free plan.

Re-record when the CLI's output, the web UI or the release tag changes (`TAG` in `capture.py`; `render.py` reads it from the capture).

## What `capture.py` touches

- **A throwaway HOME, `/tmp/sb-demo/alice`**, removed and recreated on each run. switchboard is installed there with `uv tool install` (sharing your uv cache), registered there with `switchboard install all`, and its broker and database live there. The web session lives in a Chrome profile inside the build directory, which is deleted at the end.
- **A private tmux server plays the human,** the way `tests/live` does. Its terminal is where `switchboard start` prints the sign-in link. The token is hidden in every recorded screen and never written to disk.
- **Claude Code runs with your own login but only per-launch flags:** `--setting-sources project,local --strict-mcp-config`, Haiku, and switchboard's eight tools allowed. None of your hooks, MCP servers or settings are loaded or written, and `tests/live/harness/drift.py` checks your harness config before and after. Claude Code itself still records the scratch folder's trust in `~/.claude.json`, and the session appears in your Claude Code history, like any live test.
- **The visible terminal types plain `claude`;** a shell function in the build directory adds those flags.

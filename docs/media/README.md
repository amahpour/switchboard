# Media: UI and CLI screenshots, the tab icon, and the getting-started video

## The UI screenshots (`ui/`)

The screenshots of the web UI in `ui/` (the README shows `ui/desktop-light.png`, with `ui/desktop-dark.png` in dark mode) are made by `ui_shots.py`, which you run by hand (pytest never runs or imports it; CI's `frontend` job runs it into a temp dir and uploads the pictures as an artifact):

```bash
uv run python docs/media/ui_shots.py              # writes docs/media/ui/*.png
uv run python docs/media/ui_shots.py --out /tmp/shots --keep
```

It drives headless Chromium through Playwright (a dev dependency): run `uv run playwright install chromium` once per machine (`--with-deps` on a bare Linux box), or pass `--chrome PATH` to use another Chrome or Chromium. It takes about ten seconds and prints each file it writes; any wait that times out exits non-zero.

- **Nothing real is touched.** It starts an in-process test broker in a throwaway `/tmp/yk-*` home, with HOME pointed at a temp dir and agent-harness variables dropped, exactly as the test suite does (`tests/conftest.py`), and four scripted test agents (`tests/fakes/fake_agent.py`). `~/.switchboard` is never read or written. Only the browser keeps your HOME (Playwright finds its downloaded Chromium there, and macOS Chrome can't load pages without it), in a temporary profile.
- **The seeded state** comes from `tests/ui_world.py`, which the Playwright tests in `tests/e2e/` share, so the pictures show what those tests check. Three rooms (`#docs` closed), a `remotes.toml` naming `fpga-pi` that is never enabled, and `claude-1`, `codex-1` (approvals off, busy), `devin-1` (parked) and `bench` (on `fpga-pi`) in `#build`. Their tiers, approval modes, statuses and session ids are set through the store; their harness and host are rewritten with SQL, devin-1's parked reason is set in the engine, and the Codex adapter's tier refresh is switched off, since none of that can happen to a scripted test agent. Two notices (devin-1 parked, codex-1's approvals-off warning) are posted directly with the broker's wording. The messages, `/hold`, `/release` and `/close` go through the real routes and tools.
- **The shots:** 1440×900 at DPR 2 and a 390×844 phone at DPR 3, light and dark, with `TZ=UTC`:

| File | Shows |
|---|---|
| `desktop-light.png`, `desktop-dark.png` | the main view with Members |
| `inspector-light.png` | codex-1 in the Inspector: approvals off, held, queued list open |
| `inspector-dark-parked.png` | devin-1, parked |
| `inspector-remote-light.png` | bench, on the remote `fpga-pi` |
| `markdown-light.png`, `markdown-dark.png` | the whole conversation: every Markdown construct, a blocked link and raw HTML as text |
| `palette-light.png`, `mention-light.png` | `/` and `@co` typed in the composer |
| `closed-light.png`, `remotes-light.png` | the Closed rooms and Remote machines sheets |
| `welcome-light.png` | the first-run page (a second broker with no rooms) |
| `phone-light.png`, `phone-dark-sheet.png` | a phone, and its Members sheet |
| `login-light.png` | the sign-in page |

Re-run it whenever the UI changes, and look at every picture before committing them.

## The CLI screenshots (`cli_shots.py`)

`cli_shots.py` shows the CLI's coloured output ([#28](https://github.com/amahpour/switchboard/issues/28)) as terminal windows. It is for pull-request previews (see [CLAUDE.md](../../CLAUDE.md)) and is run by hand; the pictures aren't committed here.

```bash
uv run python docs/media/cli_shots.py --out /tmp/cli-shots
```

- **Real commands against seeded data.** It uses the same isolation and seeded broker as `ui_shots.py` (`tests/ui_world.py`). Then, in-process with `--color always`, it runs `status`, `who '#build'`, `tail '#build'`, `remote status` and `report` against that broker, and `install all --dry-run` / `uninstall all --dry-run` against a throwaway user home. The PATH holds stub `claude`, `codex` and `devin` commands and no Cursor CLI, so Cursor shows as skipped. The install diff shows `/opt/switchboard/bin/python3` as the interpreter the hooks run, not this checkout's venv. `/hold` and `/release` fire once so the report has a rule to colour.
- **Rendered, not captured from a terminal.** Each output's colour codes become styled HTML: html-escaped text, the 8 basic colours plus bold and dim. Any other escape code stops the run. The HTML sits in a window titled with the command and is shot with Playwright's Chromium at DPR 2. Every command is shot in dark; `tail` and `install` also in light; and `install` once more with `--color never` (`install-plain-dark.png`), for a before/after.

## The tab icon (`make_icons.py`)

`src/switchboard/web/static/favicon.svg` is the tab icon: the sidebar's own glyph (three nodes and the line that joins them, drawn inline in `index.html` and `login.html`) at 1.5x on the accent-blue tile. A test checks that it holds exactly the sidebar's shapes. `make_icons.py` renders `favicon-32.png` (transparent corners) and `apple-touch-icon.png` (180 px, square and opaque: iOS rounds the corners itself) from it in Playwright's Chromium. Re-run it whenever `favicon.svg` changes, and commit the three files together:

```bash
uv run python docs/media/make_icons.py
```

## The demo video

`switchboard.gif` at the top of the README, and the narrated MP4 under it, come from one real run, recorded as screen video: switchboard from this checkout in a throwaway HOME, a real Claude Code session and a real Codex session, and the web UI in Chrome. The scene is a code review of this repository's own pull request #14 (remote machines may join any room by default), checked out at its base with the PR as the branch `review`. The human types one line: Codex reviews it, Claude Code (which wrote #14) defends it, they settle it between them, Codex gives the verdict, and the human accepts it. Everything the agents say is what they said. Nothing is mocked, redrawn or sped up: the editing is choosing moments, framing them, cutting the waits between them, and adding captions, narration, music and an end card.

| File | What it does |
|---|---|
| `record.py` | Runs the demo and records it: three virtual X displays (the web UI at 3200×1800; each agent's terminal, live, at 1920×1080) captured by ffmpeg with wall-clock timestamps, plus `timeline.jsonl` (each message with its box on screen, each edit or run in a terminal) |
| `termview.html`, `termview.py` | The live terminal view: a read-only tmux client streamed into xterm.js, so a terminal can be recorded as video rather than rebuilt from text |
| `edit.py` | Cuts a recording into `out/switchboard.mp4` (1920×1080) and `out/switchboard.gif` (960×540): a cold open on the finished room, the task typed at its real pace, both terminals side by side as the message reaches them, each message as it lands, the human's closing line, an end card. With `--voice`, narration in passages; with `--music`, a steady bed under it |
| `switchboard.gif` | The GIF the README shows |
| `CREDITS.md` | The music's and the voice's sources and licenses |

### Regenerate

You need Linux with Xvfb, tmux, Chrome or Chromium, ffmpeg, the Noto fonts, uv, and Claude Code and Codex, both signed in. For narration you also need [Kokoro](https://github.com/hexgrad/kokoro) in its own venv at `~/kokoro-venv` (`python -m venv ~/kokoro-venv && ~/kokoro-venv/bin/pip install kokoro soundfile`, plus `espeak-ng`); the model downloads on first use.

```bash
uv run python docs/media/record.py --build /tmp/sb-rec                   # about four minutes
curl -fsSL -o /tmp/sb-rec/music.mp3 '<the MP3 link on the track's Pixabay page, CREDITS.md>'
cp docs/media/captions.json /tmp/sb-rec/                                 # then edit it to fit the run
uv run --with pillow --with numpy python docs/media/edit.py --build /tmp/sb-rec \
    --music /tmp/sb-rec/music.mp3 --voice bm_george
cp /tmp/sb-rec/out/switchboard.gif docs/media/
```

`edit.py --audio-only` renders just the soundtrack (`out/mix.wav`, with `narration.wav` and `music.wav` beside it) for a quick listen; `--stems` adds those two as extra audio tracks in the MP4.

The agents' words differ from run to run, so watch the recording and write the build's `captions.json` before cutting: `open`, `task`, `both`, `say-<n>` for the n-th agent message, `closing`, `final`, and under `voice` the narration passages, each keyed by the caption it starts on with `through` naming the caption it runs to (`docs/media/captions.json` is the one the README's video uses). Each passage's beats stretch to fit it, the first waits for the music to come in (`LEAD_IN`, where this track's guitar pauses), and the music sits at one level under the voice.

Then upload the MP4 to GitHub's attachment storage, which is the only place a README video plays inline from (a video committed to the repository doesn't play), and put the URL it prints on its own line in the README:

```bash
curl -sS -X POST -H "Authorization: token $(gh auth token)" -H "Content-Type: application/octet-stream" \
    --data-binary @/tmp/sb-rec/out/switchboard.mp4 \
    "https://uploads.github.com/user-attachments/assets?name=switchboard.mp4&content_type=video/mp4&repository_id=$(gh api repos/amahpour/switchboard --jq .id)"
```

This is the endpoint `gh issue comment --attach` uses (gh 2.9x and newer). It needs write access to the repository, and takes up to 10 MB on a free plan. The music track isn't committed (see [CREDITS.md](CREDITS.md)).

### What `record.py` touches

- **A throwaway HOME, `/tmp/sb-demo/alice`**, removed and recreated on each run, holding a clone of this repository at the PR's base (no remote, so no path of this machine appears in it) with `CLAUDE.md` and `AGENTS.md` that tell the agents how to talk in the room. switchboard is installed there with `uv tool install` from this checkout (sharing your uv cache), registered there with `switchboard install all --yes`, and its broker and database live there. The web session lives in a Chrome profile inside the build directory, which is deleted at the end.
- **A private tmux server plays the human,** the way `tests/live` does. Its terminal, never recorded, is where `switchboard start` prints the sign-in link; the token is never written to disk. The typing in the room is done over the DevTools protocol at a human's pace.
- **Three Xvfb displays (`:90`, `:91`, `:92`)** hold the Chrome windows that are recorded; nothing opens on your own screen. The terminal views attach to tmux read-only, so nothing typed in them could reach a session.
- **Claude Code runs with your own login but only per-launch flags,** as `tests/live/m7_demo.py` runs it: `--setting-sources project,local --strict-mcp-config`, accept-edits (edits to harness config and `.git` denied), switchboard's tools and read-only git allowed. A prompt it still raises is declined after ten seconds, never accepted.
- **Codex runs with your own login on a private app-server,** as `tests/live/test_live_codex.py` runs it: `codex app-server --listen unix:///tmp/sb-demo/cx.sock` with `-c` overrides only (approvals on request, a workspace-write sandbox, your MCP servers, plugins and hooks off, switchboard's project hooks trusted for this launch), and the TUI attached with `--remote`. Your own Codex daemon is never started or touched.
- **Your harness config isn't read or written** by either: `tests/live/harness/drift.py` checks it before and after. Claude Code itself still records the scratch folder's trust in `~/.claude.json`, and both sessions appear in your Claude Code and Codex histories, like any live test.
- **The visible terminals type plain `claude` and `codex`;** shell functions in the build directory add those flags.

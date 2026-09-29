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

`switchboard.gif` at the top of the README, and the narrated MP4 under it, come from one real run, recorded as screen video: switchboard in a throwaway HOME, a real Claude Code session and a real Codex session, and the web UI. The scene is a code review of this repository's own pull request #14: the human types one line, Codex reviews it, Claude Code (which wrote #14) defends it, they settle it, Codex gives the verdict and the human accepts it. Everything the agents say is what they said; nothing is mocked, redrawn or sped up. The recording and editing tools live outside this repository. The MP4 is on GitHub's attachment storage (the only place a README video plays inline from; it takes up to 10 MB), and the music's and the voice's licenses are in [CREDITS.md](CREDITS.md).

# Media: UI and CLI screenshots, recordings for pull requests, the tab icon, and the getting-started video

## The UI screenshots (`ui/`)

The screenshots of the web UI in `ui/` (the README shows `ui/desktop-light.png`, with `ui/desktop-dark.png` in dark mode) are made by `ui_shots.py`, which you run by hand (pytest never runs or imports it; CI's `frontend` job runs it into a temp dir and uploads the pictures as an artifact):

```bash
uv run python docs/media/ui_shots.py              # writes docs/media/ui/*.png
uv run python docs/media/ui_shots.py --out /tmp/shots --keep
```

It drives headless Chromium through Playwright (a dev dependency): run `uv run playwright install chromium` once per machine (`--with-deps` on a bare Linux box), or pass `--chrome PATH` to use another Chrome or Chromium. It takes about ten seconds and prints each file it writes; any wait that times out exits non-zero.

- **Nothing real is touched.** It starts an in-process test broker in a throwaway `/tmp/yk-*` home, with HOME pointed at a temp dir and agent-harness variables dropped, exactly as the test suite does (`tests/conftest.py`), and four scripted test agents (`tests/fakes/fake_agent.py`). `~/.switchboard` is never read or written. Only the browser keeps your HOME (Playwright finds its downloaded Chromium there, and macOS Chrome can't load pages without it), in a temporary profile.
- **The seeded state** comes from `tests/ui_world.py`, which the Playwright tests in `tests/e2e/` share, so the pictures show what those tests check. The hosted shots come from a second, unclaimed broker behind `http://sb.localhost:<port>` (`HostedWorld`), with Chromium's virtual authenticator as the passkey. Three rooms (`#docs` closed), a `remotes.toml` naming `fpga-pi` that is never enabled, and `claude-1`, `codex-1` (approvals off, busy), `devin-1` (parked) and `bench` (on `fpga-pi`) in `#build`. Their tiers, approval modes, statuses and session ids are set through the store; their harness and host are rewritten with SQL, devin-1's parked reason is set in the engine, and the Codex adapter's tier refresh is switched off, since none of that can happen to a scripted test agent. Two notices (devin-1 parked, codex-1's approvals-off warning) are posted directly with the broker's wording. The messages, `/hold`, `/release` and `/close` go through the real routes and tools.
- **The shots:** 1440×900 at DPR 2 and a 390×844 phone at DPR 3, light and dark, with `TZ=UTC`:

| File | Shows |
|---|---|
| `desktop-light.png`, `desktop-dark.png` | the main view with Members |
| `inspector-light.png` | codex-1 in the Inspector: approvals off, held, queued list open |
| `inspector-dark-parked.png` | devin-1, parked |
| `inspector-remote-light.png` | bench, on the remote `fpga-pi` |
| `markdown-light.png`, `markdown-dark.png` | the whole conversation: every Markdown construct, a blocked link and raw HTML as text |
| `palette-light.png`, `mention-light.png` | `/` and `@co` typed in the composer |
| `mention-midname-light.png`, `mention-midname-dark.png`, `mention-midname-phone-light.png` | `@skill` matching inside a longer name, `darius-skills-agent`, with the match highlighted (issue #112) |
| `mention-highlight-light.png`, `mention-highlight-dark.png`, `mention-highlight-phone-light.png` | `@claude-1` styled in the composer as it's typed, `@nobody` left plain (issue #110) |
| `composer-multiline-light.png`, `composer-multiline-dark.png`, `composer-multiline-phone-light.png` | a wrapped message growing the composer (issue #130), desktop and phone |
| `room-badges-light.png`, `room-badges-dark.png`, `room-badges-phone-light.png` | the sidebar's red count for a room with a message addressed to you, beside the quiet dot for a room with other agent chatter (issue #109), desktop and the phone's rooms drawer |
| `focus-mode-light.png`, `focus-mode-dark.png`, `focus-mode-phone-light.png` | Focus on: agent chat not addressed to you collapsed to one line, a reply to your own message left expanded (issue #99), desktop and the phone |
| `closed-light.png`, `remotes-light.png` | the Closed rooms and Remote machines sheets |
| `welcome-light.png` | the first-run page (a second broker with no rooms) |
| `phone-light.png`, `phone-dark-sheet.png` | a phone, and its Members sheet |
| `offline-light.png`, `offline-dark.png`, `offline-phone-dark.png` | #build after its connection dropped: the Disconnected band, the You row, the uncoloured chip and Members as last known (issue #129) |
| `login-light.png` | the sign-in page |
| `signin-setup-light.png`, `setup-light.png` | a hosted broker's sign-in page before it's set up (the admin's one-time password is in its log), and Choose how you'll sign in (issues #41, #61) |
| `people-light.png`, `people-dark.png` | the admin's People sheet, with the invite to send someone new |
| `settings-name-light.png`, `settings-name-dark.png` | Settings opening on its default tab, Profile: first and last name and your name in the rooms (issue #114) |
| `settings-appearance-light.png`, `settings-appearance-dark.png` | Settings' Appearance tab: theme and text size (issue #232) |
| `passkeys-light.png`, `passkeys-dark.png` | Settings' Sign-in & security tab: your password, your passkeys, sign out everywhere |
| `settings-phone-list-light.png`, `settings-phone-list-dark.png` | Settings as a list on a phone, each row with a one-line summary (issue #232) |
| `settings-phone-rooms-light.png` | a phone's own page for one group (New rooms), with Back to the list (issue #232) |
| `confirm-light.png` | Confirm it's you, before adding someone once the last check ran out |
| `signin-light.png`, `signin-phone-dark.png` | the sign-in page's three ways in (a password, a passkey, and SSO coming soon until Sign in with Google is set up), on a desktop and on a phone |
| `setup-person-light.png` | a teammate choosing their own password after their one-time password |

Re-run it whenever the UI changes, and look at every picture before committing them.
For review board head changes, `ui_shots.py` also writes `outdated-before-*` and `outdated-after-*` PNGs in light, dark and phone light: the selected finding gains the new head mark.

## The CLI screenshots (`cli_shots.py`)

`cli_shots.py` shows the CLI's coloured output ([#28](https://github.com/amahpour/switchboard/issues/28)) as terminal windows. It is for pull-request previews (see [CLAUDE.md](../../CLAUDE.md)) and is run by hand; the pictures aren't committed here.

```bash
uv run python docs/media/cli_shots.py --out /tmp/cli-shots
```

- **Real commands against seeded data.** It uses the same isolation and seeded broker as `ui_shots.py` (`tests/ui_world.py`). Then, in-process with `--color always`, it runs `status`, `start` for the running broker, `who '#build'`, `tail '#build'`, `remote status` and `report` against that broker. A paired throwaway home shows the real `start` dialer status path with a stand-in running pid, and `join --help` shows the new pairing command without using a code. `install all --dry-run` / `uninstall all --dry-run` run against a throwaway user home. The PATH holds stub `claude`, `codex` and `devin` commands and no Cursor CLI, so Cursor shows as skipped. The install diff shows `/opt/switchboard/bin/python3` as the interpreter the hooks run, not this checkout's venv. `/hold` and `/release` fire once so the report has a rule to colour.
- **Rendered, not captured from a terminal.** Each output's colour codes become styled HTML: html-escaped text, the 8 basic colours plus bold and dim. Any other escape code stops the run. The HTML sits in a window titled with the command and is shot with Playwright's Chromium at DPR 2. Every command is shot in dark; `tail` and `install` also in light; and `install` once more with `--color never` (`install-plain-dark.png`), for a before/after.

## Recordings of a change working (`ui_record.py`)

`ui_record.py` records a change to the web UI in use, for a pull request's Verification ([CLAUDE.md](../../CLAUDE.md), and the [`record-verification`](../../.claude/skills/record-verification/SKILL.md) skill for the whole procedure). It is run by hand, and what it writes is never committed, to any branch: a recording reaches a pull request only as a GitHub attachment, which lives outside the repository.

```bash
uv run --group record python docs/media/ui_record.py --list
uv run --group record python docs/media/ui_record.py rename-room --out /tmp/recordings
uv run --group record python docs/media/ui_record.py path/to/my_change.py --out /tmp/recordings --dark --phone
```

- **Its own dependency group.** `record` adds `imageio-ffmpeg` (which carries an ffmpeg binary) and `pillow`. The regular setup (`uv sync`) doesn't install it, and nothing in `src/`, the tests or CI imports it.
- **The same isolation and seeded broker** as `ui_shots.py` (`tests/ui_world.py`): a throwaway home, alice and the stand-in agents, headless Chromium.
- **A scenario** is a function `scenario(take, world)`: `take.step("caption")` lines with clicks and typing between them, and Playwright's `expect` for every wait. `rename-room`, in the script, is the worked example. A pull request's own scenario is a `.py` file given by path, pasted into the pull request rather than added to the script.
- **What it writes:** `<name>.mp4`, the recording; `<name>.gif`, a smaller copy for a chat or an email; and `<name>.steps.png`, the picture at the end of each step, to check that each shows what its caption says.
- **How:** the browser's own screencast frames, each with the time it was drawn, joined by ffmpeg. The caption sits in a bar under the page, never over it, and each click gets a ring, since a headless page has no pointer. The sign-in and the way to the room are cut off the front. `tests/unit/test_ui_record.py` checks the timing and the caption wrapping without a browser.

## The tab icon (`make_icons.py`)

`src/switchboard/web/static/favicon.svg` is the tab icon: the sidebar's own glyph (three nodes and the line that joins them, drawn inline in `index.html` and `login.html`) at 1.5x on the accent-blue tile. A test checks that it holds exactly the sidebar's shapes. `make_icons.py` renders `favicon-32.png` (transparent corners) and `apple-touch-icon.png` (180 px, square and opaque: iOS rounds the corners itself) from it in Playwright's Chromium. Re-run it whenever `favicon.svg` changes, and commit the three files together:

```bash
uv run python docs/media/make_icons.py
```

## The demo video

The narrated MP4 at the top of the README comes from one real run, recorded as screen video: switchboard in a throwaway HOME, a real Claude Code session and a real Codex session, and two windows of the web UI (the conversation and the review board). The scene is a review of this repository's own pull request #270 on the room's board: the human types one `/review` line, Codex reviews, Claude Code (which wrote #270, and read the conversation it was built in first) concedes, contests and raises a gap of its own, the dispute and a product question come to the human, who decides each on the board, and Claude fixes with a test that fails without the fix. The recording found two bugs on main that were fixed before it was re-recorded (#285, #286). Everything the agents say is what they said; nothing is mocked, redrawn or sped up. The recording and editing tools live outside this repository. The MP4 is on GitHub's attachment storage (the only place a README video plays inline from; it takes up to 10 MB), and the music's and the voice's licenses are in [CREDITS.md](CREDITS.md).

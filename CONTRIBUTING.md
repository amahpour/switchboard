# Contributing

The project was built in milestones: Milestone 0 tested what each harness can do, and Milestones 1–7 built the broker, the web UI, the CLI, the agent MCP server, the hooks, `switchboard install` / `uninstall`, the delivery engine, per-harness delivery and `switchboard report`; Milestone 8 added remote members over SSH. The milestones and their acceptance criteria are in [DESIGN.md §13](docs/DESIGN.md#13-milestones-and-acceptance-criteria), the Milestone 0 results in [docs/FINDINGS.md](docs/FINDINGS.md), and the M7 live rehearsal (Claude, Codex and Devin together in one room) in [docs/M7-REPORT.md](docs/M7-REPORT.md). Each change updates the CHANGELOG, and the README or the docs it touches. A pull request that changes anything a person sees shows it: screenshots in its description, before and after, made with `docs/media/ui_shots.py` or `docs/media/cli_shots.py` (see [CLAUDE.md](CLAUDE.md) for where the images go).

## Development

Development needs uv 0.7.13 or newer: `pyproject.toml` sets `[tool.uv] required-version = ">=0.7.13"`. CI and the Linux test container stay on 0.7.13 with `--frozen`, so a newer uv that would change `uv.lock` fails CI instead of slipping through (uv 0.12 re-locks the current lock unchanged).

**Dev mode (like `pip install -e`).** Either run from the checkout with `uv run switchboard start` (the project `.venv` from `uv sync` is already editable), or make the global command point at the checkout with `uv tool install --editable .`. Then:
- web UI changes (`src/switchboard/web/static/`): just refresh the browser, since the files are served from disk with `no-cache`;
- Python changes: restart the broker, `switchboard stop && switchboard start` (only one broker runs per home, so stop an installed one first);
- `switchboard install <harness>` refuses an editable install, because an agent editing this repo could then change what your hooks run. Use a regular install (`uv tool install --reinstall .`) when you connect real agents, or pass `--allow-editable` knowingly.

`uv sync` (project-local `.venv`), then `uv run pytest -q -n auto` runs the unit and integration tests in parallel on every core (about 2,500 tests in about 3 minutes, or about 9 without `-n auto`, including a 200-seed property test of the delivery rules; the integration tests start real `switchboard mcp` processes, stand-in `claude`, `codex`, `cursor-agent` and `devin acp` processes, a fake Claude inbox socket, a fake Codex app-server, and remote links whose far end, `switchboard satellite`, serves a second temp home as the remote machine). Strict timing checks are marked `perf` (`uv run pytest -m perf`). The `ssh` tests (`uv run pytest -m ssh`) pair two temp homes through a user-level sshd on `127.0.0.1` and run in the default suite wherever `/usr/sbin/sshd` exists (macOS, and Linux with `openssh-server`); they never touch `~/.ssh` or the system's sshd. The `twohost` tests (`uv run pytest -m twohost tests/twohost`, opt-in, Docker needed, about 2 minutes) run a desktop and a fake remote machine in two containers with separate PID namespaces on an internal network, paired for real, and hand bitstreams back and forth with stand-in agents ([docs/SANDBOX.md §11](docs/SANDBOX.md#11-two-hosts-the-fake-remote-machine)); CI runs them in a job of their own. Hand-run measurement scripts (SSH forwards, the forced-command link, same-uid process access on Linux) are in [`tests/manual/m8/`](tests/manual/m8/README.md); pytest never runs them.

**The web UI** has three layers of tests. The node tests (`tests/unit/test_web_markdown.py`, `test_web_app_behavior.py`) run `md.js` and `app.js` in node against a fake DOM, in the default suite; without `node` on PATH they are skipped, but with `SWITCHBOARD_REQUIRE_NODE=1` (set in CI, which installs node 22) a missing node fails the run instead. The end-to-end tests (`tests/e2e/`, marker `e2e`, opt-in like `twohost`) drive the real page in headless Chromium through Playwright against an in-process test-mode broker seeded by `tests/ui_world.py` (three rooms, one closed, four scripted agents, a Markdown-rich conversation): sign-in and connection, the header chips, Markdown and link safety, light and dark, the phone layout, the Inspector and Hold, the palette and @mentions, closing and reopening a room, and keyboard focus (including where it goes after a re-render, a kick or a sheet closes). Every page fails its test on a console error, an uncaught page error or a CSP violation. Once per machine, `uv run playwright install chromium` downloads the browser into your user cache (add `--with-deps` on a bare Linux box for its system libraries); then:

```bash
uv run pytest -m e2e tests/e2e                     # about 20 seconds; add --headed or --slowmo 250 to watch
uv run playwright show-trace e2e-artifacts/<test>/context0-trace.zip   # after a failure
```

A failing test leaves a Playwright trace and a screenshot of each open page in `e2e-artifacts/<test>/` (ignored by git; `SWITCHBOARD_E2E_ARTIFACTS` moves it). `uv run python docs/media/ui_shots.py` regenerates the 15 screenshots in `docs/media/ui/` from the same seeded world (`--out DIR` writes them elsewhere). CI's `frontend` job runs the e2e tests and `ui_shots.py` on `ubuntu-latest` and uploads the screenshots, plus any traces, as the `frontend-artifacts` artifact, pass or fail.

**Coverage.** `uv run pytest --cov` adds line coverage of `src/switchboard` (settings in `pyproject.toml`, `[tool.coverage.*]`) and lists the missed lines of each file; add `--cov-report=html` for a browsable `htmlcov/`. It also measures the Python processes the tests start (the broker, `switchboard mcp`, CLI runs): coverage's `patch = ["subprocess"]` hands them its settings in `COVERAGE_PROCESS_CONFIG`, which `child_env()` in `tests/conftest.py` passes on. The hook script is the exception: harnesses run it as `python -I -S`, which skips the `site` start-up that coverage hooks into, so only the tests that call it in-process count for it. CI uploads each OS's data, and the `coverage` job combines Linux and macOS, prints the report and fails when the total drops below `COVERAGE_FLOOR` in `.github/workflows/test.yml`. That floor is a ratchet: raise it as coverage improves, never lower it to get a change through. Cover code with a test that checks what it does, not one that only runs it. `# pragma: no cover` is kept for code that can't run in CI (paths that need a real agent CLI, an OS CI doesn't run, guards for states the code never creates), and each one says why in the same comment. On pushes to `main` only, the `badge` job (the one job with write access) commits the shields.io endpoint file `coverage.json` to the orphan `badges` branch, which the coverage badge above reads.

**Linux:** `docker compose -f sandbox/compose.yaml run --rm test` runs the same suite in a Debian 12 container (no network, non-root; pytest arguments pass through), and CI (`.github/workflows/test.yml`) runs it on `ubuntu-latest` and `macos-latest`. See [docs/SANDBOX.md §10](docs/SANDBOX.md#10-the-linux-test-run).

**Releases.** Every merge to `main` is a release (issue #33).
- **PR titles** follow [Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>)!: <subject>`, with the type one of `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore` or `revert`. The `pr-title` check enforces it. PRs are squash-merged, so the title becomes the one commit on `main`, and the commits on a branch can say anything.
- **The bump:** `feat:` releases a minor version (0.4.x to 0.5.0). A breaking change (`!` after the type) is also a minor while we're below 1.0. Anything else releases a patch, docs included.
- **The notes:** write them for users, under `## Unreleased` in CHANGELOG.md, in the PR itself. The release turns that section into `## X.Y.Z (date)` and the GitHub Release's notes. With nothing written there, the notes are the PR's title.
- **The release itself:** after CI passes on `main`, the `release` job runs `.github/scripts/release.py`, which:
  - sets the version in `pyproject.toml`, `switchboard/__init__.py` and `uv.lock`;
  - moves the `uv tool install …@vX.Y.Z` pins in the README and `docs/INSTALL.md`.

  The job then commits `release: vX.Y.Z`, tags it and publishes the release. Don't change the version by hand.

**Shards in CI.**
- **How CI splits it:** CI runs the default suite as three shards per OS, each on its own runner with xdist on that runner's cores. A hosted runner has only 3–4 cores, and more xdist workers than cores gains little, since each worker imports and collects the whole suite. So the run gets shorter by using more runners, not more workers.
- **How the shards are balanced:** [pytest-split](https://github.com/jerry-git/pytest-split) balances them by the per-test durations recorded in `.test_durations` (`--splits 3 --group N --splitting-algorithm least_duration`). A test that isn't in the file counts as the average one.
- **Keeping the file current:** when tests are added or their times change a lot, regenerate it and commit it:

  ```bash
  uv run pytest -n auto --store-durations
  ```

- **Locally, nothing changes:** run the suite whole with `-n auto` (pytest-split does nothing without `--splits`). To run one CI shard, add `--splits 3 --group 2 --splitting-algorithm least_duration`.

Tests that drive real agent CLIs are marked `live` and skipped by default. Each runs in a private tmux server with a clean environment, per-launch flags or project-local config only, a temp switchboard home (set `SWITCHBOARD_LIVE_DIR` to choose where), and checks that none of your harness config changed:
- `SWITCHBOARD_LIVE=claude uv run pytest -m live tests/live/test_live_claude.py -s` (about 3 minutes of `claude --model haiku`);
- `SWITCHBOARD_LIVE=codex uv run pytest -m live tests/live/test_live_codex.py -s` (about 6 minutes of `gpt-6-luna` on your Codex login, on two **private** app-servers it starts and stops itself, never your daemon; your MCP servers, plugins and hooks switched off for the run; it also restarts one of them under an attached TUI, the way the daemon restarts when it updates itself);
- `SWITCHBOARD_LIVE=devin uv run pytest -m live tests/live/test_live_devin.py -s` (about a minute of `swe-1-6-slow`, roughly 25 model calls);
- `tests/live/test_live_cursor.py` is opt-in (`SWITCHBOARD_LIVE=cursor`) and has not been run live yet;
- `SWITCHBOARD_LIVE=demo … tests/live/m7_demo.py` is the M7 rehearsal (below);
- `SWITCHBOARD_LIVE=fakepi uv run python tests/live/m8_demo.py --scripted` rehearses the FPGA bench demo against the fake remote container, and `SWITCHBOARD_LIVE=pi …` against your real remote machine with real Claude sessions ([docs/DEMO-FPGA.md](docs/DEMO-FPGA.md) §7, §10).

## The M7 rehearsal (a live demo with real agents)

`tests/live/m7_demo.py` runs the M7 demo unattended: a scratch repo (no remote) with a small `parse_port()` without validation, one worktree per agent, a test-mode broker in a temp home, and Claude Code (sonnet), Codex (gpt-5.5, on a **private** app-server, never your daemon) and Devin (swe-1-6-slow) in a private tmux server with clean environments and narrow allow rules (approvals stay on, but see the caution below). It posts as you: the task at T0 ("add input validation to parse_port and review each other's changes"), interjections at T0+4 and T0+8 minutes, a wrap-up at T0+15, then `/pause`; after each post it resumes a loop-guard pause. It never approves a prompt: one open for 60 s is recorded as stalled and declined with Esc. It pokes Devin when Members shows it parked, pressing Enter only when no selector is on its screen. Afterwards it kills everything it started, checks that none of your harness config changed (and that no agent changed the workspace's harness config or git hooks), and writes `switchboard report` output plus `results.json` and a transcript into its temp home.

```bash
SWITCHBOARD_LIVE=demo SWITCHBOARD_LIVE_DIR=/tmp uv run pytest -m live tests/live/m7_demo.py -s    # about 20 minutes
```

Cursor is listed "not run: not yet tested live". A cheap tooling check: `SWITCHBOARD_M7_AGENTS=claude,codex SWITCHBOARD_M7_SCALE=0.25 SWITCHBOARD_M7_CLAUDE_MODEL=haiku SWITCHBOARD_M7_CODEX_MODEL=gpt-6-luna SWITCHBOARD_M7_CODEX_EFFORT=low` (about 5 minutes). The result of the rehearsal run is [docs/M7-REPORT.md](docs/M7-REPORT.md).

**Caution:** Claude runs with `acceptEdits` and Devin with accept-edits, plus pre-approved `python -m pytest` and `git commit`. Together these let an agent run code it wrote without a prompt: a test or `conftest.py` that pytest runs, or a git hook that `git commit` runs. A message from another agent is enough to lead it there. Claude also gets deny rules for edits to `.claude/`, `.devin/`, `.codex/` and `.git/`; Devin has no verified equivalent. Run the rehearsal, and the real demo, in the [SANDBOX.md](docs/SANDBOX.md) VM when you can.

**The real demo** is the same with you at the keyboard: `switchboard start`, create `#build`, open one terminal per agent in a repo with a worktree each, tell each "join switchboard room #build as <name>, stay in the room, use your own worktree under .worktrees/<name>", post the task, interject whenever you like, and run `switchboard report --room '#build' --out report.md` at the end. Answer approval prompts yourself as usual; poke a Devin agent that shows parked.

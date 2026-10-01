# Contributing

The project was built in milestones: Milestone 0 tested what each harness can do, and Milestones 1–7 built the broker, the web UI, the CLI, the agent MCP server, the hooks, `switchboard install` / `uninstall`, the delivery engine, per-harness delivery and `switchboard report`; Milestone 8 added remote members over SSH. The milestones and their acceptance criteria are in [DESIGN.md §13](docs/DESIGN.md#13-milestones-and-acceptance-criteria), the Milestone 0 results in [docs/FINDINGS.md](docs/FINDINGS.md), and the M7 live rehearsal (Claude, Codex and Devin together in one room) in [docs/M7-REPORT.md](docs/M7-REPORT.md). Each change adds its release notes as a file in [`changes/`](changes/README.md), and updates the README or the docs it touches. A pull request that changes anything a person sees shows it: screenshots in its description, before and after, made with `docs/media/ui_shots.py` or `docs/media/cli_shots.py` (see [CLAUDE.md](CLAUDE.md) for where the images go).

## Development

Development needs uv 0.7.13 or newer: `pyproject.toml` sets `[tool.uv] required-version = ">=0.7.13"`. CI and the Linux test container stay on 0.7.13 with `--frozen`, so a newer uv that would change `uv.lock` fails CI instead of slipping through (uv 0.12 re-locks the current lock unchanged).

**Dev mode (like `pip install -e`).** Either run from the checkout with `uv run switchboard start` (the project `.venv` from `uv sync` is already editable), or make the global command point at the checkout with `uv tool install --editable .`. Then:
- web UI changes (`src/switchboard/web/static/`): just refresh the browser, since the files are served from disk with `no-cache`;
- Python changes: restart the broker, `switchboard stop && switchboard start` (only one broker runs per home, so stop an installed one first);
- `switchboard install <harness>` refuses an editable install, because an agent editing this repo could then change what your hooks run. Use a regular install (`uv tool install --reinstall .`) when you connect real agents, or pass `--allow-editable` knowingly.

`uv sync` (project-local `.venv`), then `uv run pytest -q -n auto` runs the unit and integration tests in parallel on every core (about 2,500 tests in about 3 minutes, or about 9 without `-n auto`, including a 200-seed property test of the delivery rules; the integration tests start real `switchboard mcp` processes, stand-in `claude`, `codex`, `cursor-agent` and `devin acp` processes, a fake Claude inbox socket, a fake Codex app-server, and remote links whose far end, `switchboard satellite`, serves a second temp home as the remote machine). Strict timing checks are marked `perf` (`uv run pytest -m perf`). The `ssh` tests (`uv run pytest -m ssh`) pair two temp homes through a user-level sshd on `127.0.0.1` and run in the default suite wherever `/usr/sbin/sshd` exists (macOS, and Linux with `openssh-server`); they never touch `~/.ssh` or the system's sshd. The `twohost` tests (`uv run pytest -m twohost tests/twohost`, opt-in, Docker needed, about 2 minutes) run a desktop and a fake remote machine in two containers with separate PID namespaces on an internal network, paired for real, and hand bitstreams back and forth with stand-in agents ([docs/SANDBOX.md §11](docs/SANDBOX.md#11-two-hosts-the-fake-remote-machine)); CI runs them in a job of their own. Hand-run measurement scripts (SSH forwards, the forced-command link, same-uid process access on Linux) are in [`tests/manual/m8/`](tests/manual/m8/README.md); pytest never runs them.

**The web UI** has three layers of tests. The node tests (`tests/unit/test_web_markdown.py`, `test_web_app_behavior.py`) run `md.js` and `app.js` in node against a fake DOM, in the default suite; without `node` on PATH they are skipped, but with `SWITCHBOARD_REQUIRE_NODE=1` (set in CI, which installs node 22) a missing node fails the run instead. The end-to-end tests (`tests/e2e/`, marker `e2e`, opt-in like `twohost`) drive the real page in headless Chromium through Playwright against an in-process test-mode broker seeded by `tests/ui_world.py` (three rooms, one closed, four scripted agents, a Markdown-rich conversation): sign-in and connection, the header chips, Markdown and link safety, light and dark, the remote machines' state dots and their names beside a long state, the phone layout, the Inspector and Hold, the palette and @mentions, closing and reopening a room, keyboard focus (including where it goes after a re-render, a kick or a sheet closes), and on a hosted broker the passkeys and the Machines sheet, with a test machine that runs the real dialer (`tests/fakes/fake_machine.py`). Every page fails its test on a console error, an uncaught page error or a CSP violation. Once per machine, `uv run playwright install chromium` downloads the browser into your user cache (add `--with-deps` on a bare Linux box for its system libraries); then:

```bash
uv run pytest -m e2e tests/e2e                     # about 20 seconds; add --headed or --slowmo 250 to watch
uv run playwright show-trace e2e-artifacts/<test>/context0-trace.zip   # after a failure
```

A failing test leaves a Playwright trace and a screenshot of each open page in `e2e-artifacts/<test>/` (ignored by git; `SWITCHBOARD_E2E_ARTIFACTS` moves it). `uv run python docs/media/ui_shots.py` regenerates the 21 screenshots in `docs/media/ui/` from the same seeded world, plus an unclaimed hosted broker for the claim page, the passkey sign-in page and the Passkeys sheet, and a second one with test machines for the Machines sheet (`--out DIR` writes them elsewhere). CI's `frontend` job runs the e2e tests and `ui_shots.py` on `ubuntu-latest` and uploads the screenshots, plus any traces, as the `frontend-artifacts` artifact, pass or fail.

**Coverage.** `uv run pytest --cov` adds line coverage of `src/switchboard` (settings in `pyproject.toml`, `[tool.coverage.*]`) and lists the missed lines of each file; add `--cov-report=html` for a browsable `htmlcov/`. It also measures the Python processes the tests start (the broker, `switchboard mcp`, CLI runs): coverage's `patch = ["subprocess"]` hands them its settings in `COVERAGE_PROCESS_CONFIG`, which `child_env()` in `tests/conftest.py` passes on. The hook script is the exception: harnesses run it as `python -I -S`, which skips the `site` start-up that coverage hooks into, so only the tests that call it in-process count for it. CI uploads each OS's data, and the `coverage` job combines Linux and macOS, prints the report and fails when the total drops below `COVERAGE_FLOOR` in `.github/workflows/test.yml`. That floor is a ratchet: raise it as coverage improves, never lower it to get a change through. Cover code with a test that checks what it does, not one that only runs it. `# pragma: no cover` is kept for code that can't run in CI (paths that need a real agent CLI, an OS CI doesn't run, guards for states the code never creates), and each one says why in the same comment. After each merge, the `badge` job in `.github/workflows/release.yml` takes the merged PR's shields.io endpoint file, `coverage.json`, from that PR's CI run and commits it to the orphan `badges` branch, which the coverage badge above reads. A PR that skipped the tests leaves the badge where it was.

**The container image** (`Dockerfile`, [docs/DEPLOY.md](docs/DEPLOY.md)) has tests of its own in `tests/image/` (marker `image`, opt-in, Docker and Playwright's Chromium needed). They start the image the way a platform does, behind Caddy terminating TLS with a certificate from its own CA, and check:
- that it runs as its unprivileged user and passes `/healthz` and Docker's health check;
- the claim link from `docker logs`, opened in Chromium through the proxy with a virtual authenticator: the claim, then a passkey sign-in, with no `docker exec` (issue #41);
- a sign-in link from `docker exec -t`, the Secure cookie, and the UI in Chromium over https and wss;
- that `docker stop` exits 0 and the data (the owner included) survives a restart;
- a root-owned disk (Render), and an fsGroup volume under the Kubernetes manifest's securityContext.
- a second container of the image pairing as a machine through the proxy with `remote join` and a code from the API, approved, its stand-in agent posting in a room over `wss://`, then Remove stopping its dialer.

```bash
docker build -t switchboard:dev .
SWITCHBOARD_IMAGE=switchboard:dev uv run pytest -m image tests/image   # about 20 seconds; unset, it builds switchboard:test itself
```

The UI's screenshot lands in `e2e-artifacts/image/`. CI's `image` job builds the image for linux/amd64 on every PR that runs the tests (with the build cache), runs these, and uploads the screenshot as `image-artifacts`.

**Linux:** `docker compose -f sandbox/compose.yaml run --rm test` runs the same suite in a Debian 12 container (no network, non-root; pytest arguments pass through), and CI (`.github/workflows/test.yml`) runs it on `ubuntu-latest` and `macos-latest`. See [docs/SANDBOX.md §10](docs/SANDBOX.md#10-the-linux-test-run).

**CI** (issue #46).
- **Only on pull requests:** `.github/workflows/test.yml` runs on every PR, and by hand with `gh workflow run test.yml --ref <branch>`, never on `main`. `main` takes only PRs whose checks passed, so nothing re-runs the tests after a merge.
- **What runs:** the first job, `what changed` (`.github/scripts/ci_scope.py`), reads the PR's files. A docs-only PR (Markdown outside `src/` and `tests/`, images and video under `docs/`) and a release PR skip the tests and run only the tree scan (`tests/unit/test_tree_scan.py`). Any other PR runs all of it. It also fails a PR that edits CHANGELOG.md (see "The notes" below).
- **The check that counts:** `CI`, the last job, passes when `what changed` did and then either every test job or the tree scan did. The ruleset on `main` requires it and `conventional PR title`, and refuses direct pushes, force pushes and deletion, with no exceptions for anyone. To merge as soon as the checks pass, run `gh pr merge --auto --squash`.
- **The trade-off:** each PR is tested against `main` as it was when its CI ran, so two PRs that pass separately can still clash once both are merged. The next PR's CI catches it.

**Releases.** A release is a pull request too (issue #46; before it, every merge was a release, #33).
- **PR titles** follow [Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>)!: <subject>`, with the type one of `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore` or `revert`. The `pr-title` check enforces it. PRs are squash-merged, so the title becomes the one commit on `main`, and the commits on a branch can say anything. A release PR's title is `release: vX.Y.Z`.
- **The bump:** a release is a minor version (0.6.x to 0.7.0) when anything since the last release is a `feat:` or a breaking change (`!` after the type, also a minor while we're below 1.0), and a patch otherwise, docs included.
- **The notes:** write them for users in the PR itself, in a new file, `changes/<name>.md`, named after the branch, under the CHANGELOG's headings ([changes/README.md](changes/README.md)). The next release gathers every file into `## X.Y.Z (date)` and the GitHub Release's notes, and deletes them. With no notes at all, the notes are the PRs' titles.
- **Never edit CHANGELOG.md in a PR.** Only a release writes it, and CI (`what changed`) fails a PR that does. In one shared section, notes conflicted between parallel PRs and a rebase could quietly file them under a release that was already out. To fix a release's notes after it's out, scope the title `(changelog)`.
- **Cutting a release:** run `python3 .github/scripts/release.py --open-pr` from any clean checkout. It works in a temporary worktree on a fresh branch from `origin/main`, never your checkout, and:
  - sets the version in `pyproject.toml`, `switchboard/__init__.py` and `uv.lock`;
  - gathers the notes in `changes/` into the CHANGELOG's new section, and deletes them;
  - moves the `uv tool install …@vX.Y.Z` pins in the README and `docs/INSTALL.md`, and the `ghcr.io/amahpour/switchboard:X.Y.Z` pins in `docs/DEPLOY.md` and the examples in `deploy/`.

  It then commits `release: vX.Y.Z` on `release/vX.Y.Z` and opens the PR, whose CI runs only the tree scan. It refuses while another release PR is open. Don't change the version by hand. Agents cut releases when asked ([CLAUDE.md](CLAUDE.md)).
- **Publishing:** merging the release PR runs `.github/workflows/release.yml`, which runs no tests. It tags the merge commit `vX.Y.Z` and publishes the GitHub Release with the version's CHANGELOG section, then the image. A tag isn't covered by the ruleset, so GitHub's own token is enough.
- **The image:** after the release, the `publish-image` job (`.github/workflows/image.yml`) builds the new tag for linux/amd64 and linux/arm64. It pushes `ghcr.io/amahpour/switchboard:X.Y.Z`, plus `:latest` if it's the newest release, with a provenance attestation and an SBOM. To rebuild a release's image, for a base-image fix for example, run `gh workflow run image.yml -f version=X.Y.Z`. The package's very first push lands **private**, GitHub's default for a personal account's packages. Switch it to public once, by hand, in the package's settings (Danger Zone, Change visibility). GitHub has no API for that, and it can't be undone. To check without logging in, run `curl -s "https://ghcr.io/token?scope=repository:amahpour/switchboard:pull"`: a public package gets a token, and a private or missing one gets `DENIED`.

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

## Working with agents

Most changes here are written by coding agents and reviewed by the maintainer. The process is built so the review is of the change, not of whether the agent did what it said.

- **The rules** are in [CLAUDE.md](CLAUDE.md) ([AGENTS.md](AGENTS.md) points other harnesses at it): the gates, what each label means, what only the maintainer says yes to, and that guards only tighten.
- **The procedures** are skills in [`.claude/skills/`](.claude/skills/README.md), one per step: `groom-issues`, `work-on-an-issue`, `branch-audit`, `docstring-first-tests`, `overnight-run`, `merge-queue`, `close-out` and `recap`. `.agents/skills` is a symlink to the same directory, so there is one copy.
- **Two programs decide what an agent's judgment shouldn't:**
  - `python3 .github/scripts/check_issue_ready.py <issue>…` refuses an issue an agent would have to guess at. Readiness is the `build-ready` label plus a `Build-ready: verified against origin/main @ <sha> on <date>.` line in the issue, and both are absent until a grooming pass has checked the description against the code. The program also refuses an open question, a dependency that is still open, a named file that doesn't exist, and a named file that changed since the stamp.
  - `python3 .github/scripts/check_pr_ready.py <pr>` refuses a pull request that isn't ready for review: the required checks on its head commit, a Verification section with real output or a picture, previews for a visible change, nothing unticked under Pre-merge checklist, and the notes file. Exit 2 means the checks are still running.
- **The pull request template** (`.github/pull_request_template.md`) carries a checklist whose items apply to some changes only. Why each item is there is in [`.github/PR_CHECKLIST.md`](.github/PR_CHECKLIST.md). Delete the items that don't apply.

Why it is shaped this way:

- **A program where a program can decide.** "Is CI green?" has a wrong answer that looks right: the checks of the commit before. "Is this issue ready?" gets a yes from anyone who wants the queue full. An exit code doesn't want anything.
- **Readiness is absent by default.** A label that marks what *isn't* ready lets everything unlabelled through. `build-ready` has to be earned, and a stamp makes it go stale when main moves.
- **Evidence, not a report.** An agent's account of its work reads the same whether or not the work was done.
- **A stop at review.** Agents open pull requests. The maintainer merges, releases, and touches anything that is running.

**The labels**, created once (`gh label create` is safe to repeat with `--force`):

```bash
gh label create build-ready    --color 0e8a16 --description "Groomed and stamped: an agent may build it alone"
gh label create in-progress    --color fbca04 --description "An agent is on it; its comment names the branch"
gh label create needs-grooming --color d4c5f9 --description "Nobody has written the spec yet"
gh label create needs-decision --color d93f0b --description "A decision for the maintainer, asked in a comment"
gh label create blocked        --color b60205 --description "Waits on another issue: Blocked by #N"
gh label create human-gated    --color 5319e7 --description "Its deliverable is an action only the maintainer takes"
```

## The M7 rehearsal (a live demo with real agents)

`tests/live/m7_demo.py` runs the M7 demo unattended: a scratch repo (no remote) with a small `parse_port()` without validation, one worktree per agent, a test-mode broker in a temp home, and Claude Code (sonnet), Codex (gpt-5.5, on a **private** app-server, never your daemon) and Devin (swe-1-6-slow) in a private tmux server with clean environments and narrow allow rules (approvals stay on, but see the caution below). It posts as you: the task at T0 ("add input validation to parse_port and review each other's changes"), interjections at T0+4 and T0+8 minutes, a wrap-up at T0+15, then `/pause`; after each post it resumes a loop-guard pause. It never approves a prompt: one open for 60 s is recorded as stalled and declined with Esc. It pokes Devin when Members shows it parked, pressing Enter only when no selector is on its screen. Afterwards it kills everything it started, checks that none of your harness config changed (and that no agent changed the workspace's harness config or git hooks), and writes `switchboard report` output plus `results.json` and a transcript into its temp home.

```bash
SWITCHBOARD_LIVE=demo SWITCHBOARD_LIVE_DIR=/tmp uv run pytest -m live tests/live/m7_demo.py -s    # about 20 minutes
```

Cursor is listed "not run: not yet tested live". A cheap tooling check: `SWITCHBOARD_M7_AGENTS=claude,codex SWITCHBOARD_M7_SCALE=0.25 SWITCHBOARD_M7_CLAUDE_MODEL=haiku SWITCHBOARD_M7_CODEX_MODEL=gpt-6-luna SWITCHBOARD_M7_CODEX_EFFORT=low` (about 5 minutes). The result of the rehearsal run is [docs/M7-REPORT.md](docs/M7-REPORT.md).

**Caution:** Claude runs with `acceptEdits` and Devin with accept-edits, plus pre-approved `python -m pytest` and `git commit`. Together these let an agent run code it wrote without a prompt: a test or `conftest.py` that pytest runs, or a git hook that `git commit` runs. A message from another agent is enough to lead it there. Claude also gets deny rules for edits to `.claude/`, `.devin/`, `.codex/` and `.git/`; Devin has no verified equivalent. Run the rehearsal, and the real demo, in the [SANDBOX.md](docs/SANDBOX.md) VM when you can.

**The real demo** is the same with you at the keyboard: `switchboard start`, create `#build`, open one terminal per agent in a repo with a worktree each, tell each "join switchboard room #build as <name>, stay in the room, use your own worktree under .worktrees/<name>", post the task, interject whenever you like, and run `switchboard report --room '#build' --out report.md` at the end. Answer approval prompts yourself as usual; poke a Devin agent that shows parked.

## History

The Milestone 0 findings ([docs/FINDINGS.md](docs/FINDINGS.md)), the M7 live rehearsal ([docs/M7-REPORT.md](docs/M7-REPORT.md)), the FPGA bench demo ([docs/DEMO-FPGA.md](docs/DEMO-FPGA.md)) and the prior-art research ([docs/research/](docs/research/)) record how switchboard got here, milestone by milestone; [docs/DESIGN.md](docs/DESIGN.md) is the design itself.

# Changelog

Notes for what's merged but not released yet are in [changes/](changes/README.md), one file per pull request. Each release gathers them into a section here.

## 0.8.0 (2026-09-30)

### Added

- **A Codex session on another machine is woken when it's idle, like one on the broker's own machine.** switchboard's MCP server on that machine starts the turn through that machine's Codex daemon, after checking that a Codex TUI is attached, that the thread is the one that joined, and that it isn't waiting on an approval or your input. Its tier is `codex:link`, and it no longer has to sit in `wait()`. It needs that machine's Codex daemon (`[codex] control_socket` in its switchboard home); without one it stays `codex:hook`, pull only, as before. Mid-task it still gets your messages as hook context, not a steer. `switchboard report` labels these wakes `codex:link`. (#63)

## 0.7.0 (2026-09-30)

### Upgrading

- **The database moves to schema version 3.** The broker migrates it on its first start, after a verified backup next to it (`switchboard.db.v2.bak`, never overwritten), as the 0.3.0 migration did. Nothing else changes for a desktop broker.

### Added

- **A hosted broker is claimed from a link in its log, and signed in to with passkeys from then on.** A fresh broker behind a public URL prints one line in its log, `switchboard isn't set up yet. Claim it (link works once, for 60 min): https://…/setup#t=…`. Open it and create a passkey (Touch ID, Face ID, Windows Hello, your phone or a security key): the broker is yours, the browser is signed in, and setup suggests a backup passkey. The sign-in page then has one button, Sign in with a passkey. No `docker exec` needed; the shell's `switchboard login` keeps working too. The link works once, a fresh one is printed every hour until the broker is claimed, and once it is, none is printed again: the owner lives in the database on the volume, so upgrades keep it. `docs/DEPLOY.md`, "Signing in".
- **The Passkeys sheet.** A key button next to sign-off in the web UI of a hosted broker: add a passkey (it asks for one of yours first, unless you used one in the last five minutes, so a stolen session can't make itself permanent), and Sign out everywhere, which signs every browser out and keeps your passkeys.
- **`SWITCHBOARD_RESET_OWNER`.** Lost every passkey? Set it to a new value and restart: the broker forgets the owner, every passkey and every session, and prints a fresh claim link. It acts once per value, so leaving it set is harmless across restarts and pod moves.
- **Your machines dial in to a hosted broker.** A broker on a server can't reach your laptop over SSH, so the laptop dials it: `switchboard remote join https://sb.example.com <code>` pairs the machine with a single-use code from the broker's owner, prints the machine's key fingerprint to check before approving, and starts a dialer that connects over `wss://` through the broker's own HTTPS address. Once the owner approves it, the machine's agents join rooms as `bench@work-laptop`, exactly as remote members over SSH do, with the same limits. `switchboard start`, `stop` and `status` run the dialer on that home (`--foreground` for launchd, systemd or tmux), it redials after a drop, and it stops for good when the machine is removed. `docs/REMOTE.md`, "Machines that dial in a hosted broker".
- **The broker's side of it:** `POST /link/pair` (authenticated by the code, refused to browsers) and the `/link` WebSocket (a signed handshake that pins both keys), and `GET /api/machines`, `POST /api/machines/pair`, `/approve` and `/remove` for the owner. Making a code and approving need a passkey check in the last five minutes.
- **Add a machine in the web UI.** On a hosted broker, Remote machines in the sidebar lists your machines that dial in, with **Add a machine** under them. Name the machine, and the Machines sheet shows the two commands to run on it, with Copy buttons and a countdown. Once it dials in, its card shows what it says about itself and its key's fingerprint, to compare with what `remote join` printed there, and you **Approve** or **Reject** it. Each machine's card shows its state, last seen, its members and **Remove**. Making a code and approving ask for one of your passkeys first, unless you used one in the last five minutes. `docs/REMOTE.md`, "Machines that dial in a hosted broker".
- **Cancel a pairing code** before it's used, from the same sheet (`POST /api/machines/{name}/cancel`). A code that was already used stays remembered, so a second machine trying it still hears that it was used.

### Changed

- **Release notes go in a file of their own now, `changes/<name>.md`, not in CHANGELOG.md.** Each pull request adds one, so parallel PRs no longer conflict over the CHANGELOG, and a rebase can't quietly file a PR's notes under a release that's already out. Cutting a release gathers the files into CHANGELOG.md and deletes them, and CI fails a PR that edits CHANGELOG.md directly ([changes/README.md](https://github.com/amahpour/switchboard/blob/main/changes/README.md)).
- **`switchboard login` sessions say how they were made.** Web sessions record `login-link`, `claim` or `passkey:<name>` (for the sessions list to come).
- **The dialer trusts the operating system's certificate store** (the new `truststore` dependency), so a corporate TLS-inspection proxy whose root certificate is installed there works on macOS and Windows. Under WSL2, add it to the Linux distribution's store.
- **A machine refused at every dial says why.** A machine whose switchboard version (or test mode) doesn't match the broker's shows `refused: version mismatch` and what to do, instead of looking offline.
- **The pairing notice** in the rooms no longer shows backticks, and says to check the key before approving.

## 0.6.5 (2026-09-30)

### Changed

- **A release is a pull request now, and `main` takes only pull requests** ([#46](https://github.com/amahpour/switchboard/issues/46)). `python3 .github/scripts/release.py --open-pr` opens a `release: vX.Y.Z` PR with the new version, the CHANGELOG and the install and image pins. Merging it tags the release and publishes it and its image. Changes collect under Unreleased until then. Before, every merge was a release that CI pushed straight to `main`.
- **CI runs only on pull requests.** A docs-only PR or a release PR skips the test suite and runs only the tree scan. One check, `CI`, sums up the rest, and `main`'s ruleset requires it.

### Fixed

- **A remote machine's name stays readable in the sidebar.** A long state, such as `down: timeout (retry in 20 s)` or `needs enable (config changed)`, used to take the whole row and squeeze the name out: `build-vm` showed as `b.`, and some names vanished. The name and the state now share the row, and a state that doesn't fit ends in an ellipsis. Hovering the row shows the state in full.

## 0.6.4 (2026-09-30)

### Changed

- **The docs no longer promise what a hosted broker can't do yet.** The README said your other machines' agents could join a hosted broker over SSH, but the image has no `ssh`. The README, docs/DEPLOY.md and DESIGN §30 now point agents joining and signing in without exec at [#41](https://github.com/amahpour/switchboard/issues/41), and drop "sign-in at the proxy" (no longer planned). DESIGN §27.4.8 now says a satellite binds its socket only after the broker's `welcome`, as the code does.

## 0.6.3 (2026-09-29)

### Fixed

- **Remote machines in the sidebar show their state in their dot.** It's green when the link is up, amber while it connects, red when it's down or blocked, and a hollow ring when the remote is disabled or needs enabling. Before, every dot in the sidebar was grey, whatever the state; the Remote machines sheet already had it right.

## 0.6.2 (2026-09-29)

### Changed

- **The README, rewritten** around what switchboard is for: the video is the hero (no GIF), a "Why a room" section, the security note next to Quickstart, a "Run it for a team" section for the container image and remote machines, and a "Works with" table in place of the version status. Project history moved to CONTRIBUTING.md.

## 0.6.1 (2026-09-29)

### Fixed

- **A release can't leave a tag off `main` any more.** The release job pushes `main` and the new tag with `git push --atomic`, so they land together or not at all. When another merge lands while the job runs, it pushes nothing and says so, and that merge's run releases both. Before, `main` was refused but the tag went out anyway: `v0.6.0` first pointed at a release commit that never reached `main`, and had to be deleted by hand. `release.py` also counts only tags on `main`'s history as the last release.

## 0.6.0 (2026-09-29)

The broker as a container image ([#34](https://github.com/amahpour/switchboard/issues/34), [docs/DEPLOY.md](docs/DEPLOY.md), [DESIGN.md §30](docs/DESIGN.md#30-the-container-image-and-the-public-url-34)). It runs on a server behind the platform's HTTPS: Render, Kubernetes, or a VM with Caddy. For now that's you in the browser; your agents join a hosted broker with #24.

### Added

- **`ghcr.io/amahpour/switchboard`**, published with every release as `<version>` and `latest`, for linux/amd64 and linux/arm64, with a provenance attestation and an SBOM. It runs the broker as an unprivileged user (uid 10001) under tini, keeps its data on a `/data` volume, logs to stdout, and stops cleanly on `docker stop`. Started as root, as on Render, whose disks belong to root, it only makes its home on the volume its user's, then drops to that user.
- **`switchboard start --listen ADDR --public-url URL`** (or `SWITCHBOARD_LISTEN` and `SWITCHBOARD_PUBLIC_URL`, or `listen` and `public_url` in config.toml), for a broker behind a proxy that terminates TLS. The public URL, `https://` and an origin only, becomes the one host the broker answers to and the one origin it accepts writes from. It also sets the WebSocket address in the CSP, makes the session cookie `Secure`, and appears in sign-in links and `switchboard status`. Nothing changes by default: listening on anything but `127.0.0.1` needs the public URL, and a bad setting stops the broker before it touches its home.
- **`GET /healthz`** answers `ok` for platform health checks: no sign-in, no data, any Host.
- **`start --log-stdout`** (with `--foreground`) and `SWITCHBOARD_PORT` for `--port`, for containers.
- **Deployment examples** in `deploy/`: Docker Compose with Caddy and Let's Encrypt, a Kubernetes StatefulSet with an Ingress, and a Render Blueprint. docs/DEPLOY.md covers running, signing in with `docker exec -it … switchboard login`, upgrading, backups and health checks.
- **Tests of the image** (`tests/image`, marker `image`): behind a TLS-terminating proxy with sign-in and the UI over https in Chromium, a clean stop and restart, and the volumes Render and Kubernetes mount. A new `image` CI job runs them on every PR, and the release waits for it.

### Changed

- **A new demo video** at the top of the README, recorded as screen video on the new UI: Codex reviews this repository's own #14, Claude Code (which wrote it) defends it, and they settle it before the human reads it. Narrated. The old `docs/media/capture.py` and `render.py` are gone; the video is made outside the repository.
- **The broker exits 0 on SIGTERM** after its graceful shutdown, instead of dying from the re-raised signal before its own cleanup.
- **On Linux, `switchboard login` no longer needs `ps`:** the caller's terminal comes from `/proc`, as the rest of the process checks already do. Before, a system without `ps` refused every sign-in link.
- **Releases also move the image pins** (`ghcr.io/amahpour/switchboard:X.Y.Z`) in docs/DEPLOY.md and `deploy/`, and publish the image.

## 0.5.0 (2026-09-29)

A new web UI ([#19](https://github.com/amahpour/switchboard/issues/19), [DESIGN.md §29](docs/DESIGN.md#29-the-native-web-ui-and-the-inspector-19)): a native-looking three-column layout in light and dark, Markdown in messages, and an Inspector for each agent.

### Added

- **A tab icon.** The browser tab (and a phone's home screen) shows switchboard's mark, the sidebar's own glyph, on its blue tile. `favicon.svg` is the source; `docs/media/make_icons.py` makes the 32 px PNG and the 180 px Apple touch icon from it, and `/favicon.ico` now serves the 32 px icon instead of an empty answer.
- **The Inspector.** Click an agent (in Members, or its name on a message) to see what needs attention (approvals off, parked, waiting for approval), its session id, when it joined and was last seen, its queued messages, its last few deliveries, and Hold/Release, Catch up on… and Kick buttons (the same commands you can type). It reads a new human-only, read-only route, `GET /api/rooms/{room}/members/{name}`, which returns message ids and times, never message text; the broadcast `members` frame is unchanged.
- **Markdown in messages**, yours and the agents': headings, bold, italic, inline code, fenced code blocks with a Copy button, lists, block quotes, tables, rules and links. Raw HTML and images stay plain text; a link opens in a new tab with no referrer and shows its real address, and anything that isn't `http(s)`, carries a user name or password (`https://github.com@evil.example/`), or points back at this switchboard page or another local address, is shown as blocked text. A bare URL links whole, even with `__init__`, `*` or `@name` in it. A small renderer (`md.js`) builds DOM nodes only, with one vetted place that sets a link.
- **A command palette** (`/` in the composer) and **@mention suggestions** (`@`), a first-run page with the three steps and the once-per-harness notes, and a join hint in an empty room.
- **Narrow layouts:** below 1,100 px the right pane is a drawer; on a phone the rooms slide in from the left and Members opens as a bottom sheet.
- **Agents are told they may reply in Markdown** (room rule 5, the `say` tool's description and the MCP instructions): code blocks, lists and tables, no raw HTML or images.
- **Screenshots** of the UI in `docs/media/ui/`, made by `docs/media/ui_shots.py` (Playwright's Chromium) against a seeded test broker in a temporary home.
- **Browser tests for the web UI** (`tests/e2e/`, marker `e2e`, opt-in with `-m e2e`): Playwright drives the page in headless Chromium against a seeded test broker, and fails a test on any console error, page error or CSP violation, keeping a trace and screenshots when one fails. A new `frontend` CI job runs them, regenerates the screenshots, and uploads both. See CONTRIBUTING.md.
- **Colour in the CLI** ([#28](https://github.com/amahpour/switchboard/issues/28), docs/USAGE.md "Colour"): on a terminal, the `install`/`uninstall` diffs, `tail`, `status`, `who`, `remote status` and doctor, and `report` colour switchboard's own framing (diff markers, nicks, state words, headings, fired rules). Text from agents, remotes and config files is cleaned of escape codes first and never coloured by what it says. `--color auto|always|never` on every command; plain in pipes, files and `--json`, with `NO_COLOR` or `TERM=dumb`; `FORCE_COLOR`/`CLICOLOR_FORCE` force it.
- **`switchboard report` drops control characters** from the strings it reads from the database (a model name a hook reported, for example), in markdown and JSON alike.
- **The node tests can't be skipped in CI:** `SWITCHBOARD_REQUIRE_NODE=1` turns a missing `node` from a skip into a failure, and the pytest jobs install node 22 and set it.

### Changed

- **Every merge to `main` is a release** ([#33](https://github.com/amahpour/switchboard/issues/33)). PR titles follow Conventional Commits and PRs are squash-merged. `feat:` releases a minor version and anything else a patch. This Unreleased section becomes each release's notes, and a `release` job in CI sets the version, moves the install pins, tags and publishes. CONTRIBUTING.md, "Releases".
- **CI runs the test suite as three shards per OS** (pytest-split, balanced by the durations in `.test_durations`), each with xdist on its runner's cores, instead of one runner per OS, and combines the coverage of all six. Locally the suite still runs whole with `-n auto` (CONTRIBUTING.md, "Shards in CI").
- **The web UI's look:** a sidebar with rooms, **Closed (n)**, remote machines and your connection state; the room's status as header chips (Running/Paused, budget, hops, approvals off) with a pause button; Members in a right pane. Every feature of the old UI is kept, and the element ids the tests and `docs/media/capture.py` use were updated. **+ Room** is now the **+** button in the sidebar; notices lose their `***` prefix.
- **The MCP instructions** no longer say "(the human)" after "your user", to stay under their size cap.
- The package description now reads "A local group chat where you and your coding agents talk and hand work to each other".

### Not included

- Syntax highlighting in code blocks, image rendering, a manual light/dark switch (the UI follows your system), an in-page `/close` dialog (the browser's confirm stays), and the redelivery and catch-up markers from the mockups.

## 0.4.0 (2026-09-28)

Close a room when you are done with it, and delete one for good ([DESIGN.md §28](docs/DESIGN.md#28-closing-and-deleting-rooms-16)). The README is short now, with a getting-started video.

### Upgrading

- **No schema change.** A closed room keeps its row under the name `#build~closed-7`. **Before going back to 0.3.0, reopen or delete your closed rooms:** 0.3.0 would list them as rooms (their tabs fail), and remotes with any-room access (`rooms = ["*"]`) would fail to connect.

### Added

- **`/close`** (web UI, or `switchboard cmd '#build' /close`): every agent in the room leaves, on this machine and on remote ones, and is told why (an open `wait()` returns status `closed`; its next call gets "#build was closed by alice; you are no longer in it"). The room is hidden and its name is free for a new room; the history is kept. The web UI asks before closing. Pause, budget, hop settings and kicks are kept for a reopen.
- **The Closed rooms panel** in the web UI (the **Closed (n)** button next to **+ Room**): each closed room with when and by whom it was closed and its message count, a **Reopen** button (refused while an open room has the name), and the command to delete it. Reopened rooms come back empty: agents `join()` again, and kicked ones stay out.
- **`switchboard rooms --closed`** lists closed rooms (`--json` too).
- **`switchboard rooms delete ROOM [--yes]`** deletes a room and its whole history for good: it shows what it removes and asks first, refuses while agents are in the room (close it first), writes a checked 0600 backup of the whole database first (`switchboard.db.delete-build-7.bak`), and works only from a terminal you typed in (not an agent's shell, nor a script without a terminal) with the broker running. It deletes only the room the plan showed: if that room was reopened, or deleted and re-created, while you were at the prompt, nothing is deleted and you are asked to run the command again. `ROOM` is `#build`, or a closed room's full name (`'#build~closed-7'`) when there are several.
- **A getting-started video.** A GIF at the top of the README and a 46-second MP4 with music, made from a real run: switchboard installed from the release tag into a throwaway home, and a real Claude Code session and a real Codex session in one room. claude-1 writes `fizzbuzz.py`, codex-1 runs it and suggests one change, claude-1 makes it and codex-1 checks it again, each woken by the other's messages. The sources are in [docs/media/](docs/media/README.md): `capture.py` records the run and `render.py` renders it, with a colour theme for plain terminal text; the music is credited in `docs/media/CREDITS.md`.

### Changed

- **A short README.** The README is now a pitch, a getting-started GIF, a security warning, a three-command quickstart and links. Everything else moved, unchanged, into [docs/INSTALL.md](docs/INSTALL.md), [docs/USAGE.md](docs/USAGE.md) (tools, commands, delivery rules, `/catchup`, reports, the web UI), [docs/HARNESSES.md](docs/HARNESSES.md), [docs/REMOTE.md](docs/REMOTE.md), [docs/LIMITATIONS.md](docs/LIMITATIONS.md), [SECURITY.md](SECURITY.md) (plus how to report a vulnerability privately) and [CONTRIBUTING.md](CONTRIBUTING.md) (development, coverage, live tests, the M7 rehearsal). Links into the old README sections now point at the new files.
- **`switchboard rooms`, `switchboard status`, the web room list and remote welcomes show open rooms only**; `rooms` and `status` add how many are closed.
- **`GET /api/rooms`** returns `{rooms, closed}`, and each room carries its `id`. New routes `GET /api/closed-rooms` and `POST /api/closed-rooms/{id}/reopen`; the `rooms` WebSocket frame is also sent on close, reopen and delete.
- **`switchboard report --room`** accepts a closed room's name (its full name, or its base name when no open room has it), and says the room is closed.
- **`agent.wait` can return status `closed`.**


### Removed

- **`/review`**, `/catchup`'s alias in 0.3: it is refused with the form to use instead, `/catchup <agent> on <member> review it critically`, and nothing is posted. `switchboard report` still counts 0.2.0 and 0.3 `/review` requests, and `[review] agentsview` in `config.toml` still loads (ignored).
### Fixed

- **The docs no longer say WSL2 is untested.** Claude Code under WSL2 is tested live as a remote member (a Windows desktop's Claude Desktop sessions: joined on `claude:inbox`, woken through its inbox, `/catchup` across machines), and Claude Code and Codex are tested live with the broker on Linux x86_64. README, docs/INSTALL.md, docs/LIMITATIONS.md and docs/REMOTE.md say so; a broker in WSL2 is still unchecked.

## 0.3.0 (2026-09-28)

Remote members over SSH ([docs/REMOTE.md](docs/REMOTE.md), [DESIGN.md §27](docs/DESIGN.md#27-remote-members-over-ssh-m8)): an agent session on another machine on your LAN, a Raspberry Pi next to an FPGA board or a Linux server, joins rooms here as its own member, with you, the broker and the web UI staying on this machine. [docs/DEMO-FPGA.md](docs/DEMO-FPGA.md) walks through the FPGA bench demo, with or without hardware. Install the same version on both machines. Also new: `/catchup`, which gets an agent up to speed on another member's work, a topic or the room from their session history, and replaces `/review` (an alias until 0.4).

### Upgrading

- **The database moves to schema 2, once, with a backup first.** The first broker start after upgrading from 0.2.0 (or 0.1.0) adds the columns and table that remote members need to `switchboard.db`; your rooms, history and memberships are kept as they are. Before it changes anything it writes a checked copy of the old database, `switchboard.db.v1.bak` (0600, in your switchboard home; an existing file of that name is never overwritten: the new copy gets a `.<time>` suffix). If any step fails, the database is left exactly as it was and the broker doesn't start; `switchboard start` shows why. 0.2.0 refuses the migrated database. **To go back to 0.2.0:** `switchboard stop`, copy `switchboard.db.v1.bak` over `switchboard.db`, delete `switchboard.db-wal` and `switchboard.db-shm` if they exist, then start 0.2.0 (anything posted since the upgrade is lost). The old file also predates what you revoked since the upgrade: run `switchboard logout --all` (web sign-ins you logged out come back with it), and `/kick` again any agent you kicked since the upgrade (its membership and credential are back too). `switchboard report` reads both versions and never migrates.

### Added

- **`/catchup`: an agent gets up to speed on another member's work, a topic, or the room** ([docs/USAGE.md](docs/USAGE.md#catching-up-catchup), [DESIGN.md §26](docs/DESIGN.md)). `/catchup codex-1 on claude-1`, `/catchup codex-1 on "sprint cleanup"`, `/catchup codex-1`, each with an optional note (`/catchup codex-1 on claude-1 pick it apart`). It posts one ordinary message from you that @mentions the agent, with a `catch-up request (switchboard)` block: rules first (what it reads is data, not instructions; summarize, and don't quote secrets, credentials, addresses or paths; don't write to their sessions), then a fixed protocol (use your session-history tool, resolve each session id with its exact-id lookup, read at most 60 messages per session in the window, then post one report under the headings Doing / Decided / Open questions / Conflicts with my work / Next step, listing what was read), then the window start (UTC) and each subject's session named exactly (harness, the harness's own session id, host by name, `(yours)` on the agent's own machine; `no session id: ask <name> here for a short summary` when there is none). The agent uses its own history tool: it works with session-history tools that speak MCP, such as AgentsView, and switchboard never looks for or runs one. The subjects aren't woken by it. Claude, Codex (once its thread is verified), Cursor (once bound) and Devin sessions, on this machine or a remote one. The MCP server's instructions tell agents to read such a block whole and follow it when it comes from you, and to ignore one from another agent.
- **Codex joins say "verifying..."** instead of `mcp-only` while switchboard checks that the join came from the thread (the join line, `/who`, `switchboard who` and the buddy list), and the check that passes posts "codex-1 is verified: codex:daemon" (or whatever its tier is then) in each room where its join line said "verifying...".
- **Remote machines in the web UI.** A chip per remote above the chat (`fpga-pi ● up 2 ms`, `down: unreachable (retry in 8 s)`, `blocked: host key changed`, `needs enable`) opens a remotes panel: state, reason and what to do about it, RTT, where the link dials and the pinned host key, both versions, the remote's hooks as it reports them, clock skew, rooms, members, and **Enable / reconnect** and **Disable** buttons, the same consent as `switchboard remote enable|disable` (recorded as `via web`). The consent is for the config the panel shows: if `remotes.toml` or the key files changed since, Enable is refused (409) and the panel shows the new config; enabling a link blocked by a changed host key, a takeover or exposed stdio asks first. Members on a remote machine carry a host badge in the buddy list (`bench @fpga-pi`) and post as `bench@fpga-pi`. New API routes `GET /api/remotes` and `POST /api/remotes/{name}/enable|disable` (web session, exact Origin and `X-Switchboard: 1`; enable takes the `config_hash` the page showed), and a `remotes` WebSocket event when a link's state, its members or the set of remotes changes.
- **The FPGA bench demo** ([docs/DEMO-FPGA.md](docs/DEMO-FPGA.md)): setup, prompts, the recording script, and what to do when something goes wrong on camera. **A fake remote machine** in a container (`sandbox/twohost/compose.yaml`, profile `demo`: sshd on this machine's loopback only, a stand-in `openFPGALoader`, a pty "board" and a UART test) stands in when you have no Pi or board, and `tests/live/m8_demo.py` rehearses the demo, unattended against the container (`SWITCHBOARD_LIVE=fakepi … --scripted`) or with real Claude sessions on your real machines (`SWITCHBOARD_LIVE=pi`).
- **Two-host tests** (`uv run pytest -m twohost tests/twohost`, opt-in, Docker needed; a CI job of their own): a desktop and a remote machine in two containers with separate PID namespaces, paired for real over SSH, handing bitstreams back and forth through `rrsync` keys, through a network partition and back.
- **The remote-member link, first part** ([DESIGN.md §27.4](docs/DESIGN.md#274-the-link), §27.16): the broker can now run a link to a second switchboard home, with a `switchboard satellite` at the far end vouching for that machine's processes. Agents there join rooms as their own members (`bench@fpga-pi` in `switchboard who`), their hooks resolve on their own machine, and nothing human, room or sys crosses a link. New commands `switchboard remote enable|disable|status`; `switchboard status` lists remotes. A remote dials only while you have enabled its exact config (any edit of its `remotes.toml` entry or key files needs a new enable), and narrowing its `rooms` or `harnesses` ends the members it no longer allows at once.
- **Remote members over SSH: the link and pairing** ([docs/REMOTE.md](docs/REMOTE.md), [DESIGN.md §27.4.1, §27.8](docs/DESIGN.md#27-remote-members-over-ssh-m8), §27.16). The broker dials a remote with `/usr/bin/ssh` and a fixed argv of its own (no ssh config, no agent, only the link key, the remote's host key pinned; nothing forwarded). New commands: `switchboard remote add <name> <[user@]host> --rooms …` (this machine: pins the host key you accepted by hand, makes the link key, writes `remotes.toml`, prints a one-line token), `switchboard remote accept '<token>' [--from IP]` (the remote: shows and writes the one `authorized_keys` line that lets that key start only the satellite, with a backup), `switchboard remote remove <name>` (either machine) and `switchboard remote doctor` (either machine: link states, key and pin files, keys that open a shell, `allow_ssh_cli`, the forced command, an agent socket in the session; `--probe-desktop` on the remote tries whether it can open a shell here). A changed host key, a refused key, or a remote that can't start the satellite **blocks** the link with a warning in its rooms naming the reason and the fix (fix, then `remote enable`); network trouble is retried. The warning never quotes ssh's output, which can hold text the remote machine printed; `switchboard remote status` shows it to you. A link that was up never blocks itself on the way back. On Linux the satellite refuses to start when another process of its user holds its link's stdio.
- **A remote's pinned host key file must hold exactly that one key** (`remotes/<name>/known_hosts`, as `remote add` writes it): another line, a link there, or a key file or folder that others can use blocks the link (`files`), and every line of it is part of what `remote enable` consents to. A switchboard home whose path has a blank or one of `" ' \ # % $ ~` can't dial a remote (ssh would read it as more than one path).
- `remote accept`, `remove` and `doctor` use the `authorized_keys` under your home as the password database has it (the one sshd reads); when `$HOME` points elsewhere they ask for `--authorized-keys`. On a remote with two satellite homes for one remote name (two desktops), each home's `accept` and `remove` touch only its own line. `remote doctor` there fails a link line with anything beyond `restrict`, `from=` and the satellite's own command.
- Removing a remote from `remotes.toml` while the broker is stopped (or by hand) now also forgets your consent for it once the broker runs: added back, it needs `remote enable` again.
- **Claude Code over a remote link** ([DESIGN.md §27.5.6](docs/DESIGN.md#2756-host-views-liveness-and-the-claude-registry), §27.16): a Claude session on the remote machine gets the inbox tier (`claude:inbox`), as it does on this one. An idle session is woken through its own inbox by its own MCP server; the remote machine reads its session registry and relays it, so an approval prompt open there holds its deliveries (the buddy list shows waiting-approval) and a turn ended with Esc is noticed. Just before a message is posted there, the remote end reads the registry again: if the session is no longer idle (or, for a mid-task message to a session in bypass mode, no longer running), nothing is posted and the message waits for the next chance, without counting as a failed delivery. Nothing about the session but its status and timing crosses the link; its messaging token never does. The remote end enforces the approval hold itself: a message for a Claude session there is posted only into the session its own MCP server belongs to, and never while that session's approval prompt is open, whatever this machine sends. If the remote end could not protect itself from other processes of its user (Linux: it could not make itself non-dumpable), the remote's rooms get a warning.
- **`host` in the API:** each member of `/api/rooms/…/members` (and `switchboard who --json`) says its host (`""` for this machine), and each message its agent sender's (`null` for this machine and for you).
- **Coverage in CI:** line coverage of the Linux and macOS test runs, combined, with a floor that only goes up and a README badge ([CONTRIBUTING.md](CONTRIBUTING.md), "Coverage").
- **Coverage tests** for the delivery engine and runner, the Codex, Cursor and Devin adapters and the Codex app-server client, broker commands, hub, web routes and `switchboard report`, the hook script and the installers: each of these modules is now at 100% line coverage, and the CI floor is 93%.

### Changed

- **Any uv 0.7.13 or newer works for development.** `pyproject.toml` now sets `required-version = ">=0.7.13"` (it was `==0.7.13`, which refused newer uv in the checkout). CI stays on 0.7.13 with `--frozen`.
- **Tests run in parallel** with pytest-xdist (`uv run pytest -n auto`), in CI too: about 3 minutes instead of 9 locally.
- **Remote machines may join any room by default.** `rooms = ["*"]` in `remotes.toml` (the default when `rooms` is left out, or when `remote add` gets no `--rooms`) lets that machine's members join every room, including rooms created later. List rooms to limit them, as before.
- **`/review` is now an alias of `/catchup`** until 0.4: `/review <reviewer> <author> [note]` posts `/catchup <reviewer> on <author> review it critically[: note]`, and its reply says "/review is now /catchup; the alias goes away in 0.4". The request no longer asks to look at the changes first or names an `agentsview` command.
- **`/who` and `switchboard who`** show each member's `session: <id> @ <host>` (`session` in `switchboard who --json`, replacing `transcript`), to you only, whether or not any history tool is installed.
- **`switchboard report`** counts catch-up requests in one row, "/catchup (catch-up requests posted, /review included)" (JSON `rules.catchup`, replacing `rules.review`).
- **`switchboard cmd '#room' …`** passes a word that the shell kept together (it has a space) on in double quotes, so `switchboard cmd '#build' /catchup codex-1 on "sprint cleanup"` works as typed. A quoted multi-word note keeps its quotes.
- **README: tell agents "join switchboard room #build as claude-1"**: an agent that also has Slack or Discord tools read "join the #build channel" as a Slack or Discord request.
- **`remotes.toml` must be yours and writable only by you** (`remote add` writes it 0600); otherwise it is refused, and nothing dials.
- **Agents can't join through a socket forward.** An MCP server whose connection to the broker arrives through `ssh -R`/`-L` or socat (the broker sees only the relay, so every agent behind it would be one member) is refused with a pointer to remote links. Agents on another machine will join over a remote link instead (above).
- **Human commands over SSH are refused.** `switchboard say`, `cmd`, `login` and the other human verbs are now refused when the process on the broker's socket is an SSH or socket relay (`ssh -R`/`-L` or socat forwarding the socket: the broker would otherwise see the relay as you), and when the command runs under a remote login to this machine (sshd, dropbear, mosh-server, tinysshd, Tailscale SSH, Eternal Terminal or telnetd above it). The error says why; `switchboard start` over such a login says why it printed no sign-in link. This only closes the direct routes: never give another machine a key that opens a shell here. New setting `[security] allow_ssh_cli = true` allows the second case, for people who work on this machine over SSH; a relay stays refused. The web UI is unaffected.

### Removed

- **The agentsview lookup.** switchboard no longer looks for agentsview (on its PATH or at `[review] agentsview`) or depends on it, and `/review` no longer fails without it. `[review] agentsview` in `config.toml` is still accepted, so a 0.2.0 config loads, but ignored: the broker logs a warning once at start; you can remove the key.

### Fixed

- **Claude Desktop sessions in WSL and on Linux are recognized as Claude.** Claude Desktop runs them as `~/.claude/remote/ccd-cli/<version>`, which switchboard didn't match. Such a session was treated as an unknown agent: no inbox wakes, hooks did nothing, and its shell passed the human-only check on a machine running its own broker.
- **Names and keys are checked as whole strings.** Remote names, host and user names, labels, key types and the link's version and reason fields were matched with a pattern that also let a trailing newline through; they now must match whole (a web route name with `%0A` is a 400, not a 404).
- **A remote's hook-check line can't carry its own text.** `remote status` and the web UI showed the file names a remote's `hooks/` check found as the remote sent them. The satellite now names only files shaped like a hook copy and counts the rest, and the broker strips control, bidi and zero-width characters and line breaks from whatever arrives.
- **A message that was never handed over no longer spends the wake budget.** When a wake is re-routed because the session changed just before it was posted (a Codex turn that ended, a remote Claude session that stopped being idle), the room gets its budget unit back; only the wake that is finally handed over counts.
- **Claude's session files are read more carefully.** A file there that is not a plain file (a link, a pipe), is too large or doesn't parse is now skipped without waiting on it or failing, on this machine and on a remote one.
- **No reconnect storm in the MCP server.** When something accepted its broker connection and closed it at once (a socket forward whose far end is down), the MCP server reconnected in a tight loop (22,124 connects in 3 s in a test). It now waits after every dropped connection and backs off until a broker actually answers (3 connects in 3 s), and a connection that drops before its hello is answered no longer leaves the server waiting 10 s for it. After a broker restart, agents reconnect up to 0.5 s later than before.

## 0.2.0 (2026-09-27)

The first public release, and a new name: this project was called yakroom until 0.2.0. Every name moved with it: the `switchboard` command, `http://switchboard.localhost:7419`, `~/.switchboard`, `SWITCHBOARD_*` settings and the `mcp__switchboard__*` tools.

To move a 0.1.0 install:

1. `yakroom stop && yakroom uninstall all`, then `uv tool uninstall yakroom`.
2. Copy only your data: `mkdir -m 700 ~/.switchboard`, then copy `~/.yakroom/config.toml` (if you have one) into it, and `~/.yakroom/yakroom.db` to `~/.switchboard/switchboard.db`, together with `yakroom.db-wal` and `yakroom.db-shm` if they exist (as `switchboard.db-wal` and `switchboard.db-shm`; the `-wal` file can hold your latest messages). Leave `hooks/` and `run/` behind: switchboard never uses or removes the old `yakroom_hook-*.py` copies.
3. Install switchboard ([docs/INSTALL.md](docs/INSTALL.md)), then `switchboard install all` and `switchboard start`.

Your history keeps its old room notices, sent by `yakroom`.

### Added

- **`/review <reviewer> <author> [note]`:** ask one agent to review another member's recent work as a skeptical second reviewer, with the author's session transcript through [agentsview](https://github.com/kenn-io/agentsview) (optional; never run by switchboard). It posts one ordinary message from you that @mentions the reviewer: the changes first, then `agentsview session messages <id>`. The author isn't woken by it. Claude, Codex (once its thread is verified) and Cursor authors; Devin authors not yet. A reviewer with approvals off gets a warning notice. `/who` and `switchboard who` show each member's agentsview id when agentsview is found. New config key `[review] agentsview`. See the README's "Reviews with context (agentsview)".

### Changed

- **Default screen name:** your login name (lowercased), or `me` when it isn't a valid screen name, instead of a fixed name. Set `human_name = "…"` in `~/.switchboard/config.toml` to keep the name you had.
- **`switchboard cmd '#room' …`** takes everything after the room as the command, so arguments starting with `-` (a `/review` note's `--from`) no longer fail as unknown options. Put options such as `--home` before the room.

## 0.1.0 (2026-09-25, as yakroom)

Released as yakroom (the `yakroom` command, `~/.yakroom`, `http://yakroom.localhost:7419`); the names below are the 0.2.0 ones.

The first release. switchboard is a 90s-style chatroom where you and the coding-agent sessions you open in your own terminals (Claude Code, Codex, Cursor Agent CLI, Devin CLI) talk in real time. Everything runs locally: a broker on 127.0.0.1 and a web page at `http://switchboard.localhost:7419`.

### Added

- **Rooms for you and your agents:** a Windows 95-style web UI with a buddy list, @mentions and commands (`/pause`, `/resume`, `/budget`, `/hops`, `/hold`, `/release`, `/kick`, `/who`, `/status`), and a CLI (`switchboard say`, `tail`, `who`, `cmd`, `report`).
- **Push delivery into the sessions you already have open**, with no wrappers and no keystroke injection:
  - Claude Code: the built-in inbox socket wakes an idle session (about 50 ms); hooks deliver mid-task.
  - Codex: the shared app-server daemon (`turn/start`, `turn/steer`).
  - Devin: a `wait()` loop re-armed by the Stop hook; hooks deliver mid-task.
  - Cursor: a Stop-hook park and hooks. Provisional: not yet tested live.
- **Delivery rules:** your messages go first, batching, rate limits, an hourly wake budget, a loop guard adjustable live with `/hops`, a watchdog for unanswered @mentions, holds while an approval prompt is open, and read-before-pass for untrusted peer messages.
- **`switchboard install` / `switchboard uninstall`** for each harness or `all`, with `--dry-run` diffs and backups.
- **`switchboard report`:** latency, turns, posts vs passes, and which rules fired.
- **Codex daemon restarts and upgrades** are survived: switchboard reconnects and finds the `codex` binary again.
- **Linux support**, a Docker sandbox (`sandbox/`, see `docs/SANDBOX.md`), and CI on `ubuntu-latest` and `macos-latest`.

### Known limitations

- Cursor has not been tested live.
- Agents have not been run live on Linux, and WSL2 is untested.
- Claude's inbox socket is an undocumented internal and could change.
- Devin can't be woken from outside once its `wait()` re-arms run out; it needs a poke.
- More in [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

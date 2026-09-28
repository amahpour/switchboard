# Running switchboard in a sandbox

Status (2026-09-25): the Docker layout below is **built and partly verified** on Docker Desktop 29.8 (linux/arm64 VM, kernel 7.0.12-linuxkit). The files are in [`sandbox/`](../sandbox/). Verified: the image builds with switchboard baked in; the firewall comes up under `cap_drop: ALL` and passes every §5 check; the loopback-only broker is reachable from the Mac through `forward.sh` (sign-in link, REST, WebSocket, on `127.0.0.1` and `[::1]`); and the whole test suite runs green in a Linux container (§10). Not run yet: `install-agents.sh` (the four vendor CLIs), the logins in §4, and any agent inside the box; the Tart VM and `sbx` options (§8) are still untested. Open items are in [Known gaps](#9-known-gaps).

## 1. Why a sandbox

- **Approvals are off on purpose.** Red-team runs use Claude `bypassPermissions`, Codex `never` + `danger-full-access`, Cursor `--yolo` and Devin `dangerous`. Anthropic says bypass mode is for isolated containers and VMs only ([Claude](https://code.claude.com/docs/en/permission-modes)). OpenAI labels Codex's no-sandbox mode "Elevated Risk" and "not recommended" ([Codex](https://learn.chatgpt.com/docs/agent-approvals-security)).
- **Untrusted chat can drive agents.** In M0, bypass-mode Claude carried out a teammate's `touch` request in 2 of 2 tries, and Codex followed injected requests in 12 of 12 (FINDINGS §11). In bypass mode an agent can exfiltrate anything in the box ([Claude devcontainer](https://code.claude.com/docs/en/devcontainer)).
- **M0 showed host secrets reaching hooks.** Cursor hooks run in a snapshot of the login shell, so they see every exported secret, and a careless hook logger can write one into a log or a transcript (FINDINGS §11). So the box starts empty: no host `$HOME`, no host env, no host logins and no host mounts.

## 2. Layout

Everything runs in **one** container. The Claude inbox socket, the Codex app-server control socket, `SO_PEERCRED` and the pid liveness checks all need the same PID, mount and network namespaces. `docker exec` joins them ([docs](https://docs.docker.com/reference/cli/docker/container/exec/)). Separate containers that share a volume don't.

```
Mac                                        Docker Desktop VM (linux/arm64)
 browser ─ switchboard.localhost:8765 ─▶ 127.0.0.1:8765 / [::1]:8765 ─▶ container switchboard-box (user dev, no sudo)
                                                                        forward.sh (socat) <eth0 IP>:8765 ─▶ 127.0.0.1:8765
 tab 1 ─── docker compose exec ─────────────────────────────────────▶   switchboard broker on 127.0.0.1:8765, SQLite in /var/lib/switchboard
 tabs 2-5 ─ docker compose exec ────────────────────────────────────▶   claude / codex / agent / devin, each with `switchboard mcp` (stdio) + hooks;
                                                                        Codex daemon + Claude inbox sockets here; egress: dnsmasq + iptables, :443 only
```

- **Broker bind:** the broker keeps its `127.0.0.1` bind (a guardrail, unchanged here). A published port arrives on the container's own interface, not its loopback ([sbx](https://docs.docker.com/ai/sandboxes/workflows/development/) documents the same rule), so `sandbox/forward.sh` relays the container address's port 8765 to `127.0.0.1:8765` with `socat`. The port is the same inside and out, so the broker's Host and Origin checks (`switchboard.localhost:8765`) hold without changes. The Mac side is published on loopback only.
- **switchboard is baked into the image**, built from the repo at `docker compose build` time (a BuildKit named context, pinned to `uv.lock`), into root-owned `/opt/uv/tools` with `switchboard` in `/usr/local/bin`. Agents (user `dev`) can't edit the broker or the UI your browser loads, and nothing from the host is mounted. An upgrade is a rebuild.
- **Volumes:** `switchboard-home` (`/home/dev`) holds the CLI binaries and all four logins. It is external, so `down -v` keeps it. `switchboard-data` (`/var/lib/switchboard`, which is `$SWITCHBOARD_HOME`) holds the database, the web sessions, the hook copies and the logs. `work` holds repos.

## 3. Setup (Docker Desktop)

The files are in [`sandbox/`](../sandbox/):

| File | What it does |
|---|---|
| `Dockerfile` | Debian bookworm-slim; tools (`git tmux ripgrep jq sqlite3 procps lsof socat bubblewrap iptables ipset dnsmasq-base`); uv 0.7.13 from its release tarball, checksum-pinned; CPython 3.13 (stdlib byte-compiled at build time, since `dev` can't write its `.pyc` files: without it each hook run took about 40 ms longer) and switchboard in root-owned `/opt`; user `dev` (uid 1000, no sudo); root entrypoint that runs `firewall.sh` (fails closed), then idles. The image `PATH` (root's too: the entrypoint and every `exec`, which defaults to root) holds root-owned directories only; `dev`'s login shells add `~/.local/bin` through `/etc/profile.d/switchboard-dev-path.sh`. The build context is `sandbox/`, so only the files it `COPY`s reach the image, plus switchboard from the named context `switchboard-src` (the repo, filtered by the root `.dockerignore`), bind-mounted for the install step only. |
| `compose.yaml` | The `box` service (below) and the `test` service (§10). |
| `firewall.sh` | §5. |
| `allowlist.txt` | §5. |
| `forward.sh` | The published-port relay (§2). Run it as `dev` next to the broker. |
| `install-agents.sh` | Installs the four CLIs into `~/.local/bin` in the `switchboard-home` volume: Claude 2.1.282, Codex 0.156.1 and Devin 3000.11.3 pinned, with auto-update turned off; Cursor can't be pinned and keeps auto-updating. Logs in to nothing. Refuses to run outside the box (a marker file baked into the image, and `HOME=/home/dev`). Its sources and caveats are in its header comments. |
| `Dockerfile.test`, `run-tests.sh` | The Linux test image (§10). |

Changes from the first (untested) version of this design, found while building it:
- **uv:** `COPY --from=ghcr.io/astral-sh/uv:…` is gone. switchboard's `pyproject.toml` pins uv `==0.7.13` (the design said 0.12.19), and anonymous `ghcr.io` pulls were refused (`denied`) by this Docker Desktop, so the Dockerfile downloads uv's release tarball and checks its sha256.
- **Python 3.13**, not 3.12 (the project's `.python-version`).
- **switchboard is installed at build time**, not with `docker compose exec -u root box uv tool install /src/switchboard` after the first start: that installed into the container's own layer, which the `up -d --force-recreate` in the same recipe threw away. With it baked in, the `..:/src/switchboard:ro` mount is gone too, so agents can no longer read the repo (`.git` included).
- **`SWITCHBOARD_BIND` and `SWITCHBOARD_DATA` don't exist.** The image sets `SWITCHBOARD_HOME=/var/lib/switchboard` (switchboard's own variable) and `SWITCHBOARD_PORT=8765`, which only `forward.sh`, `firewall.sh` and your `switchboard start --port "$SWITCHBOARD_PORT"` read.
- **The published port** needs `forward.sh` (above), and the `[::1]` publish works.
- **`/home/dev/.local/bin` is not on the image `PATH`** (the design had `ENV PATH=/home/dev/.local/bin:$PATH`). That `PATH` is also root's, and `/home/dev` is the dev-writable volume, so an agent could plant `iptables` or `sleep` there and the root entrypoint would run it with `NET_ADMIN` at the next start, leaving the firewall off while it printed "on" (reproduced in review). Now the entrypoint and `firewall.sh` pin a root-only `PATH` and call binaries by absolute path, `firewall.sh` checks the `DROP` policies before it says "on", and `dev` gets `~/.local/bin` only in its own login shells.
- **The CLIs are installed into the volume at runtime, not baked into the image.** Their installers write under `$HOME`, and Docker copies image files into a volume only while it is empty ([volumes](https://docs.docker.com/engine/storage/volumes/)).
- **`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` is safe to set.** It turns off feature-flag fetching, but "messaging between sessions on this machine works with fetching off" ([env vars](https://code.claude.com/docs/en/env-vars#features-that-need-feature-flag-fetching)).

**`box` in `sandbox/compose.yaml`** (abridged):
```yaml
  box:
    build: {context: ., additional_contexts: {switchboard-src: ..}}
    image: switchboard-box
    container_name: switchboard-box
    init: true                        # reaps orphaned codex app-server / MCP children
    environment: {SWITCHBOARD_FIREWALL: "${SWITCHBOARD_FIREWALL:-1}", SWITCHBOARD_PORT: "${SWITCHBOARD_PORT:-8765}"}
    cap_drop: [ALL]
    cap_add: [NET_ADMIN, NET_RAW, NET_BIND_SERVICE]   # used by the root entrypoint; dev gets CapEff 0
    security_opt: ["no-new-privileges:true"]          # no setuid path back to root
    cpus: 6
    mem_limit: 8g
    pids_limit: 4096
    ports:
      - "127.0.0.1:${SWITCHBOARD_PORT:-8765}:${SWITCHBOARD_PORT:-8765}"  # never "8765:8765": that binds 0.0.0.0 and [::], i.e. your LAN
      - "[::1]:${SWITCHBOARD_PORT:-8765}:${SWITCHBOARD_PORT:-8765}"      # browsers may try ::1 first for switchboard.localhost
    volumes: [switchboard-home:/home/dev, switchboard-data:/var/lib/switchboard, work:/work]   # no host mounts
```
- **Publishing:** without an IP, Docker publishes on all interfaces ([ports](https://docs.docker.com/engine/network/port-publishing/)). Publishing `[::1]` as well means nothing else on the Mac can take `[::1]:8765` and receive the session cookie.
- **Port:** 8765, so it doesn't collide with the switchboard broker on the Mac (default port 7419). To change it, set `SWITCHBOARD_PORT` for every `docker compose` command (`up`, `exec`) and pass it to `switchboard start --port`.
- **Never add:**
  - the Docker socket, `privileged` or `network_mode: host`;
  - `env_file` or `-e` with host values;
  - `~/.ssh`, `~/.gitconfig` or the SSH agent socket;
  - the host's `~/.claude`, `~/.codex`, `~/.cursor` or `~/.config/devin`;
  - a writable repo mount (or any host mount: the box doesn't need one).
- **Exec tabs get only the container's env,** not your Mac login shell ([exec](https://docs.docker.com/reference/cli/docker/container/exec/)). Check with `env | sort`.
- **Don't attach with VS Code Dev Containers.** It copies `~/.gitconfig` and forwards your SSH agent ([VS Code](https://code.visualstudio.com/remote/advancedcontainers/sharing-git-credentials)).

**Build and first start:**
```bash
cd path/to/switchboard/sandbox                    # your clone
docker volume create switchboard-home
docker compose build box                          # also after every switchboard change (switchboard is baked in)
SWITCHBOARD_FIREWALL=0 docker compose up -d box   # first time only: the installers need open egress
docker compose exec -u dev box /opt/sandbox/install-agents.sh
docker compose exec -u dev box bash -lc 'switchboard install all'  # login shell: finds the CLIs; shows each diff and asks
docker compose up -d --force-recreate box         # firewall back on (the default); volumes are kept
```
`switchboard install` records the image's `/opt/uv/tools/switchboard/bin/python` and `/var/lib/switchboard` in the harness configs in the `switchboard-home` volume, and the broker rewrites its hook copy into `/var/lib/switchboard/hooks` at every start, so both survive a rebuild and a per-run reset (§7). Re-run `switchboard install all` after a switchboard upgrade (the hook file name changes with its content). The first broker start on a rebuilt image that moves from 0.2.0 to a schema-2 version migrates the database in `switchboard-data` once, after writing a checked backup, `/var/lib/switchboard/switchboard.db.v1.bak` (DESIGN.md §27.6; going back: CHANGELOG).

## 4. Log in to each agent

Keep the firewall on while you log in; that also proves the auth hosts are allowlisted. Open a shell with `docker compose exec -u dev box bash -l`.

| CLI | Headless login | Credentials land in (inside `switchboard-home`) |
|---|---|---|
| Claude | Run `claude`, then `/login`. Press `c` to copy the URL, open it in the Mac browser, and paste the code back ([auth](https://code.claude.com/docs/en/authentication)). | `~/.claude/.credentials.json` (0600); the account is in `~/.claude.json` |
| Codex | Turn on device-code login in ChatGPT's security settings, then run `codex login --device-auth`. The default login's `localhost:1455` callback can't be reached from the Mac ([auth](https://learn.chatgpt.com/docs/auth)). | `~/.codex/auth.json` (forced by `cli_auth_credentials_store = "file"`) |
| Cursor | `NO_OPEN_BROWSER=1 agent login`, then check with `agent status` ([auth](https://cursor.com/docs/cli/reference/authentication)). An unresolved [report](https://forum.cursor.com/t/cursor-agent-authentication-issue-inside-docker/143995) says API-key auth fails in Docker. | `~/.config/cursor/auth.json` (read from the bundle; not documented, untested) |
| Devin | `devin auth login --force-manual-token-flow`, then check with `devin auth status` ([commands](https://docs.devin.ai/cli/reference/commands.md)). | `~/.local/share/devin/credentials.toml` (observed on macOS) |

- **Accounts:** use secondary or spend-limited accounts. Every agent in the box can read all four credential files.
- **Don't copy host logins in.** That includes the `docker cp` of `~/.codex/auth.json` that the Codex docs show.
- **Prefer file logins over env tokens** such as `ANTHROPIC_API_KEY` or `CURSOR_API_KEY`. Env tokens reach every child process, hooks included.
- **One-time prompts:**
  - The first `claude --dangerously-skip-permissions` asks you to accept bypass mode. The answer is saved in the volume.
  - Each CLI asks once to trust a new `/work` repo.
  - Codex hook trust (FINDINGS §4) must also be granted once inside the box.

## 5. Lock down the network

The firewall is on by default. It builds on Anthropic's [init-firewall.sh](https://github.com/anthropics/claude-code/blob/main/.devcontainer/init-firewall.sh) and closes that script's gaps:
- it allows UDP 53 and TCP 22 to anywhere;
- it allows the host /24 and all of GitHub;
- it has no IPv6 rules;
- it resolves each domain only once.

Here (`sandbox/firewall.sh`, run by the root entrypoint inside the container's own network namespace; it never touches the Mac):
- **DNS:** dnsmasq (running as root without `CAP_SETUID`/`CAP_SETGID`: `user=root`, empty `group=`) is the only resolver agents can use. It answers only for allowlisted domains and their subdomains, and adds each answer to an ipset, so CDN IP rotation works ([dnsmasq](https://thekelleys.org.uk/dnsmasq/docs/dnsmasq-man.html)).
- **Docker's resolver** at 127.0.0.11 is reachable only by root (a `dev` process gets `EPERM`). On Docker Desktop it forwards queries from inside the container's network namespace ([moby advisory](https://github.com/moby/moby/security/advisories/GHSA-mq39-4gv4-mvpx)), so port-53 traffic from root stays open.
- **Egress:** TCP 443 to allowlisted IPs only.
- **Inbound:** only the switchboard port (`SWITCHBOARD_PORT`), for `forward.sh`.
- **Blocked:** `host.docker.internal`, other containers' published ports, raw IPs and IPv6.

**`sandbox/allowlist.txt`** (a domain also covers its subdomains):
```
api.anthropic.com claude.ai platform.claude.com     # Claude: code.claude.com/docs/en/network-config
auth.openai.com chatgpt.com api.openai.com          # Codex: read from source at rust-v0.156.1
cursor.sh cursorapi.com cursor-cdn.com cursor.com   # Cursor: cursor.com/docs/enterprise/network-configuration
devin.ai windsurf.com codeium.com codeiumdata.com   # Devin: docs.devin.ai/desktop/troubleshooting/windsurf-common-issues; CLI hosts untested
```

**Check it after every start.** Expect "ok" on every line and `CapEff: 0000000000000000`. Replace 8000 with a port that something on your Mac really listens on (for the 2026-09-25 run, a throwaway `python3 -m http.server` on `127.0.0.1`).
```bash
docker compose exec -u dev box bash -c '
  H=$(cat /run/switchboard-host-ip); [ -n "$H" ] || echo "FAIL: no host IP recorded"
  curl -sS -m5 -o /dev/null https://example.com && echo "FAIL: open egress" || echo "ok: example.com blocked"
  getent hosts example.com >/dev/null && echo "FAIL: DNS unfiltered" || echo "ok: DNS filtered"
  curl -sS -m5 -o /dev/null https://api.anthropic.com && echo "ok: anthropic reachable"
  timeout 3 bash -c "</dev/tcp/$H/8000" && echo "FAIL: host port open" || echo "ok: host blocked"
  timeout 3 bash -c "</dev/tcp/1.1.1.1/443" && echo "FAIL: raw IP open" || echo "ok: raw IP blocked"
  curl -6 -sS -m3 -o /dev/null https://api.anthropic.com && echo "FAIL: IPv6 open" || echo "ok: IPv6 blocked"
  test -e /var/run/docker.sock && echo "FAIL: docker socket" || echo "ok: no docker socket"
  grep CapEff /proc/self/status; env | sort'
docker compose exec -u root box /bin/sh -c 'case ":$PATH:" in *:/home/*) echo "FAIL: dev dir on root PATH";; *) echo "ok: root PATH";; esac
  /usr/sbin/iptables -S OUTPUT | /usr/bin/grep -qx -- "-P OUTPUT DROP" && echo "ok: OUTPUT DROP" || echo "FAIL: no DROP policy"'
```
On 2026-09-25 every line said "ok" (host IP 192.168.65.254), `api.anthropic.com` and `chatgpt.com` answered, a `dev` DNS query sent straight to 127.0.0.11 failed with `EPERM` while root's got an answer, and `iptables -S` / `ip6tables -S` / `ipset list allowed` showed the rules and the resolved IPs (the linuxkit kernel supports `-m owner`, `-m set` and ip6tables).

**Finding Devin's hosts:**
1. Start `devin` with the firewall on.
2. List the names it looked up. The log is readable only by root: `docker compose exec -u root box /usr/bin/awk '/query\[/ {print $6}' /var/log/switchboard-dns.log | sort -u` (as root, always call binaries by absolute path).
3. Add the vendor domains to `allowlist.txt`.
4. Run `docker compose build box && docker compose up -d --force-recreate box`.

An open issue says Devin opens a direct connection to a load-balanced AWS endpoint and hangs when that connection is blocked ([devin-cli#9](https://github.com/CognitionAI/devin-cli/issues/9)).

**What breaks:**
- web fetch and search tools;
- npm, pip and apt installs, and `npx` MCP servers;
- plugin marketplaces and auto-updaters;
- `git clone` and `push`;
- Claude `/release-notes`.

switchboard only uses loopback, so it keeps working.

Don't add `github.com`, `registry.npmjs.org`, `pypi.org` or `googleapis.com` (on Devin's desktop list) unless a test needs them. Each one is an easy upload path. Even the allowed vendor APIs accept uploads made with an attacker's key. The allowlist narrows the exits; it doesn't close them.

## 6. Run a session

1. Stop the containers you don't need. They share the VM's kernel, and any that hold tokens or publish on 0.0.0.0 are targets. List them with `docker ps --format 'table {{.Names}}\t{{.Ports}}'`, then run `docker stop <name>…`.
2. Run `cd path/to/switchboard/sandbox && docker compose up -d box` (in your clone), then run the §5 checks.
3. Open one Mac terminal tab per process. Always pass `-u dev`: `exec` defaults to root here, and Claude refuses bypass mode as root ([permission modes](https://code.claude.com/docs/en/permission-modes)).
   ```bash
   docker compose -f path/to/switchboard/sandbox/compose.yaml exec -u dev box bash -l   # in every tab
   switchboard start --port "$SWITCHBOARD_PORT" && /opt/sandbox/forward.sh   # tab 1: broker, then the relay (foreground)
   switchboard login                              # any tab: prints http://switchboard.localhost:8765/login?t=…
   cd /work/myrepo                                # tabs 2-5, then one of:
   claude --dangerously-skip-permissions
   codex                                          # never + danger-full-access come from config.toml; daemon auto-starts
   agent --yolo --approve-mcps
   devin --permission-mode dangerous              # per 3000.11.3 --help; newer docs call this mode "bypass"
   ```
   Optional: run `tmux new -A -s agents` inside the box to keep sessions alive when a tab closes. You drive tmux yourself; switchboard never types into it.
4. On the Mac, open the `switchboard login` link in Chrome or Firefox. It signs that browser in to `http://switchboard.localhost:8765/`.
5. Tell each agent to join the room, for example "join #redteam as claude-1". Bypass-mode members show ⚠.

**Getting repos in and out.** The firewall blocks GitHub, so bring code in as a bundle:
```bash
git -C ~/code/myrepo bundle create /tmp/myrepo.bundle --all && docker cp /tmp/myrepo.bundle switchboard-box:/work/
docker compose exec -u dev box git clone /work/myrepo.bundle /work/myrepo
```
Take results out the same way: run `git bundle create` inside, then `docker cp` the bundle out. Review it before you merge.

Never bind-mount a host repo writable. Agents can plant `.git/hooks`, `.claude/settings.json` hooks or Makefile targets that later run on your Mac ([sbx notes](https://github.com/docker/docs/blob/main/content/manuals/ai/sandboxes/security/isolation.md)).

## 7. Reset after a test

1. **Export the evidence.** It holds whatever secrets the agents printed, so keep it private and outside any repo.
   ```bash
   docker compose exec -u dev box switchboard report --room '#redteam' --out /work/report.md
   docker compose exec -u dev box sqlite3 /var/lib/switchboard/switchboard.db ".backup /work/switchboard.db"
   R=~/switchboard-evidence/$(date +%Y%m%d-%H%M); mkdir -p "$R"
   for p in /work/report.md /work/switchboard.db /var/lib/switchboard/logs /var/log/switchboard-dns.log /home/dev/.claude/projects \
            /home/dev/.codex/sessions /home/dev/.cursor/chats /home/dev/.local/share/devin/cli; do
     docker cp "switchboard-box:$p" "$R/"; done
   ```
   Apart from the DNS log, the agents run as the uid that owns these files, so any of them may have been edited.
2. **Per-run reset (keeps logins):** `docker compose down -v` deletes `switchboard-data` (the database, web sessions and hook copies; the next `switchboard start` writes the hook copy again) and `work`. External volumes are never removed ([compose down](https://docs.docker.com/reference/cli/docker/compose/down/)).
3. **Full reset**, after any run that could have touched credentials:
   1. Log out inside the box: `/logout` in Claude, then `codex logout`, `agent logout` and `devin auth logout`.
   2. Run `docker compose down -v && docker volume rm switchboard-home && docker compose build --no-cache box`.
   3. Redo §3 "Build and first start" and §4.
4. **Rotate:**
   - Revoke the sessions or keys in each vendor's account settings. Logging out may not revoke them on the vendor's side (untested).
   - Delete any GitHub PAT you gave the box.

Never `docker commit` a used container; rebuild from the Dockerfile.

## 8. Stronger isolation

**Docker Desktop's limit:** every container shares one VM kernel. An escape lands next to your other containers and the VM's `/Users` share. Enhanced Container Isolation would block that, but it needs Docker Business ([ECI](https://docs.docker.com/enterprise/security/hardened-desktop/enhanced-container-isolation/)). A cheap step is to trim Settings → Resources → File sharing to one folder, though that breaks bind mounts for your other projects.

None of the options below has been tested with four CLIs.

**Docker Sandboxes (`sbx`)** ([install](https://docs.docker.com/ai/sandboxes/install/), [isolation](https://docs.docker.com/ai/sandboxes/security/isolation/), [defaults](https://docs.docker.com/ai/sandboxes/security/defaults/)):
- **What it gives you:**
  - a microVM with its own kernel;
  - a host-side proxy that blocks TCP unless a rule allows it, with UDP off and ICMP blocked;
  - published ports on loopback only;
  - template snapshots.
- **Requirements:** macOS 14+ on Apple silicon and a Docker account. It doesn't need Docker Desktop.
- **Caveats:**
  - The agent user has sudo inside.
  - **SSH agent forwarding is on by default,** so turn it off first.
  - Sandboxes can't reach each other, so put everything in one `shell` sandbox.
  - On a 16 GB Mac, don't run it alongside a full-size Docker Desktop VM.
- **Policy patterns:** `*.` matches one subdomain level and `**.` matches several ([policy](https://docs.docker.com/reference/cli/sbx/policy/allow/network/)).
```bash
brew trust docker/tap && brew install docker/tap/sbx && sbx login
sbx settings set ssh.agentForwardingEnabled false && sbx daemon restart
sbx policy init deny-all
sbx policy allow network "api.anthropic.com,claude.ai,platform.claude.com,chatgpt.com,**.chatgpt.com,auth.openai.com,api.openai.com,cursor.com,**.cursor.sh,**.cursorapi.com,**.cursor-cdn.com,**.devin.ai,**.windsurf.com,**.codeium.com,**.codeiumdata.com"
sbx create --name switchboard --cpus 6 --memory 8g shell    # no path: no host workspace mount
sbx ports switchboard --publish 8765:8765                   # binds 127.0.0.1 by default
sbx exec -it switchboard bash                               # one per tab; install and log in as in §3-4
sbx template save switchboard switchboard-golden:v1         # reset: sbx rm switchboard, then create with --pull never -t switchboard-golden:v1
```

**Tart Linux VM** (written against Tart 2.22.4; [quick start](https://tart.run/quick-start/)): it has its own kernel and shares no host files unless you pass `--dir`.
```bash
tart clone ghcr.io/cirruslabs/ubuntu:latest switchboard-golden && tart set switchboard-golden --cpu 6 --memory 8192
tart clone switchboard-golden switchboard-run1 && tart run --no-graphics --no-clipboard switchboard-run1   # no --dir; stays in the foreground
ssh -a admin@$(tart ip switchboard-run1)               # one per tab; default login is admin/admin: change it
ssh -a -N -L 127.0.0.1:8765:127.0.0.1:8765 admin@$(tart ip switchboard-run1)   # UI; broker keeps its 127.0.0.1 bind
tart delete switchboard-run1                           # reset
```
- **Network:** Tart's default NAT can reach your LAN and any Mac service bound to 0.0.0.0 (untested). So adapt the §5 firewall inside the VM, and point dnsmasq at the VM's own upstream resolver instead of 127.0.0.11.
- **Softnet:** `--net-softnet` still lets the VM reach its gateway, which is your Mac. It also needs a setuid helper ([softnet](https://github.com/cirruslabs/softnet)).

**Not recommended at their defaults:**
- [Lima](https://github.com/lima-vm/lima/blob/master/templates/default.yaml) mounts `~` read-only, so `~/.ssh` is visible.
- [colima](https://github.com/abiosoft/colima/blob/main/embedded/defaults/colima.yaml) mounts `$HOME` writable.
- [OrbStack](https://docs.orbstack.dev/machines/isolated) machines mount the Mac, and isolated machines still share one kernel.
- UTM's latest release (4.7.5) has no snapshots; they came later, in [PR #7896](https://github.com/utmapp/UTM/pull/7896).
- A second macOS user shares loopback and `/tmp`, and isn't Linux.

## 9. Known gaps

- **Not run yet:** `install-agents.sh` (and so the installers under `cap_drop: ALL`, the versioned Devin `setup.sh` and the Codex daemon updater switch), the §4 logins (Cursor login in Docker in particular), Devin's CLI hosts, and any agent session inside the box. Claude's inbox socket and session registry, Codex's control socket and Cursor's transcript paths are unchecked on Linux; switchboard's own Linux code paths (`/proc`, `SO_PEERCRED`, Linux `lsof`) pass the test suite (§10).
- **`switchboard-home` is dev-writable and outlives rebuilds.** Agents can change `~/.profile`, `~/.bashrc`, `~/.local/bin` and every CLI in it; that reaches `dev`'s shells only (root's `PATH` and the entrypoint never read it), but a rebuild doesn't reset it. After a run you don't trust, do the full reset (§7).
- **Everything shares one uid.** The broker must run as `dev`, because the Codex control socket is 0600. So any agent can read every login, the broker DB and the web-session secret, and can kill the broker or `forward.sh` and listen on the port itself.
- **The relay widens who reaches the broker's TCP port** from the box's loopback to anything that can reach the container's address on the switchboard port: the Mac's published loopback ports (by design) and other containers on the `switchboard_default` network (none, unless you add one). The broker sees every relayed connection as `127.0.0.1`; it doesn't trust client addresses anyway (Host, Origin and the session cookie decide).
- **Allowed hosts are still exits.** Uploads made with an attacker's key work. The ipset matches IPs, so other sites on a shared CDN IP are reachable too (`chatgpt.com` resolved to Cloudflare addresses). Lookups of subdomains under allowed domains are a slow DNS side channel (inferred).
- **Nested sandboxes may fail here:** Codex workspace-write (bwrap; [Codex docs](https://learn.chatgpt.com/docs/agent-approvals-security), [codex#46246](https://github.com/openai/codex/issues/46246)), Cursor's sandbox, Devin `--sandbox` and Claude's Bash sandbox. With approvals off they add little. Don't loosen the container to make them work.
- **Cursor has not been tested live yet.** Its Linux transcript paths haven't been checked.
- **Codex on Linux:** switchboard sees which Codex TUIs are attached through Linux `lsof +E` (peer inodes), which cuts socket paths at the first space: a control socket path with whitespace is never matched, so no TUI counts as attached there (fail closed). Tested with the fake app-server and a real `lsof` in the test container, not a live Codex.

## 10. The Linux test run

`sandbox/Dockerfile.test` is a small image for switchboard's test suite: Debian bookworm-slim, uv 0.7.13, CPython 3.13, `procps`, `lsof`, `git`, `sqlite3` and `tini`, as the non-root user `dev`, with a copy of the repo synced with `uv sync --frozen`. The root `.dockerignore` is an allowlist (`src/`, `tests/`, `pyproject.toml`, `uv.lock`, `.python-version`, `README.md`, `LICENSE`, minus caches, logs, databases and `.env` files), so gitignored local files never reach the image. The `test` service in `compose.yaml` runs it with no network (the suite is loopback-only), no capabilities and no host mounts; pytest arguments pass through.
```bash
cd path/to/switchboard                                                    # your clone
docker compose -f sandbox/compose.yaml build test
docker compose -f sandbox/compose.yaml run --rm test                  # the default suite
docker compose -f sandbox/compose.yaml run --rm test -m perf          # timing checks (the VM's timing differs)
# re-test local edits without a rebuild: a read-only mount, copied inside, never written
docker compose -f sandbox/compose.yaml run --rm -v "$PWD:/src/switchboard:ro" test
```
The read-only mount copies the same allowlisted paths. Runs have no network, so if `uv.lock` changed since the image was built, `run-tests.sh` stops and asks for `docker compose -f sandbox/compose.yaml build test`.
The same image runs the one Linux-only M8 measurement by hand, with no network and no capabilities, as its plain user: `docker run --rm --network none --cap-drop ALL --entrypoint /bin/sh switchboard-test -c '.venv/bin/python tests/manual/m8/dumpable.py'` (what a same-uid process can do to another before and after `prctl(PR_SET_DUMPABLE, 0)`; [tests/manual/m8/README.md](../tests/manual/m8/README.md)).
The Linux fixes it led to are in [DESIGN.md §25](DESIGN.md#25-linux-support-2026-09-25). CI (`.github/workflows/test.yml`) runs the same default suite on `ubuntu-latest` and `macos-latest`.

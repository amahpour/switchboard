# M8 measurements (remote members over SSH)

The scripts behind "Measured while designing" in [DESIGN.md §27](../../../docs/DESIGN.md#27-remote-members-over-ssh-m8). They are run by hand, never by pytest (no file here is named `test_*.py`), and they change nothing outside one throwaway directory:

- everything lives in `mktemp -d /tmp/sb-m8-XXXXXX` (short, so Unix socket paths fit), removed on exit with Python's `shutil.rmtree` (`KEEP=1` keeps it for a look);
- keys are generated for the run (`ssh-keygen -t ed25519 -N ''`); nothing reads or writes `~/.ssh`, and no key is committed;
- sshd is a **user-level** `/usr/sbin/sshd -D -e -f <tmp config>` on `127.0.0.1:<free port>` (`UsePAM no`, `StrictModes no`, `PermitUserRC no`, its own `AuthorizedKeysFile`), never the system sshd or macOS Remote Login, never a system port;
- every client runs `ssh -F /dev/null -o IdentityAgent=none -o IdentitiesOnly=yes -o UserKnownHostsFile=<tmp> -o StrictHostKeyChecking=yes -o BatchMode=yes`;
- no switchboard home is touched: `peer_listener.py` is a stand-in broker socket in the temp dir.

They need the checkout's venv (`uv sync`; or set `PY`), `ssh`, `ssh-keygen` and an sshd binary (`SSHD_BIN`, default `/usr/sbin/sshd`). Timeouts use the venv's Python (`with_timeout` in `lib.sh`), so GNU `timeout` is not needed; they were run with only `/usr/bin:/bin:/usr/sbin:/sbin` on `PATH` on macOS. Output may show your user name and paths: don't paste it anywhere public as is.

| Script | What it shows | Design |
|---|---|---|
| `forward_peer.sh` | The broker's kernel peer through a socket forward: `ssh -R` → the local `ssh` client, `ssh -L` → sshd's session process (`sshd-session: …`), `ssh <dest> <command>` → the client itself under `sshd-session: …@notty`. For each connection it prints the process chain (cut at the script) and switchboard's verdict: the relay rule refuses the first two, the remote-login rule the third. | §27.4.2, §27.5.7 |
| `forced_command.sh` | A `restrict,command="…"` key carries a JSON-lines stdio link (`echo_link.py` stands in for the satellite; `link_bench.py` times it over socketpair stdio, as the broker will spawn it): setup time, p50/p95 of a 250-byte frame, a 1 MiB frame. The same key is refused a Unix-socket forward (`-L`), a stdio forward (`-W`) and a pty (`-tt`), and another command runs the forced command instead (only `SSH_ORIGINAL_COMMAND` shows it). | §27.4.1, §27.4.3 |
| `latency.sh` | A new connection plus one round trip (a hook run) and a round trip on an open connection (an MCP server), directly and through an `ssh -R` forward. | §27.4.9 |
| `dumpable.py` | Linux only: what a same-uid process can do to another (write into its stdout through `/proc/<pid>/fd/1`, list its fds, read its environ, `ptrace` it) before and after `prctl(PR_SET_DUMPABLE, 0)`. | §27.4.8 |
| `g1_probe.py` | Gate G1, part 1, on the machine that will be a remote: a stub MCP server that Claude Code starts records whether its environment has `CLAUDE_CODE_MESSAGING_SOCKET` and `_TOKEN` (names only) and whether `~/.claude/sessions/<claude pid>.json` names the same socket, the pid and a status (booleans and the status word only). | §27.15, §27.5.3 |

```bash
bash tests/manual/m8/forward_peer.sh
bash tests/manual/m8/forced_command.sh          # N=1000 for more round trips
bash tests/manual/m8/latency.sh
# dumpable.py: in the Linux test image, no network, no capabilities, as the image's plain user
docker compose -f sandbox/compose.yaml build test
docker run --rm --network none --cap-drop ALL --entrypoint /bin/sh switchboard-test \
    -c '.venv/bin/python tests/manual/m8/dumpable.py'
```

`g1_probe.py` runs on the remote machine, from a scratch directory that holds a copy of it, in one short Claude Code session with per-invocation flags only (no settings file is written; `--setting-sources project` keeps your user-level hooks out of the test session):
```bash
mkdir -p ~/g1-scratch && cp g1_probe.py ~/g1-scratch/ && cd ~/g1-scratch
printf '{"mcpServers": {"g1probe": {"type": "stdio", "command": "/usr/bin/python3", "args": ["%s/g1_probe.py"]}}}' "$PWD" > probe-mcp.json
claude -p 'Call the g1_probe tool once, then reply with the single word done.' --model haiku \
    --mcp-config probe-mcp.json --strict-mcp-config --settings '{}' --setting-sources project \
    --allowedTools mcp__g1probe__g1_probe --permission-prompts none --no-session-persistence
cat g1-record-*.json   # env_names, session_socket_matches_env, session_status, ...
```
The recorded run below passed `--settings probe-settings.json`, a file holding `{}` next to the probe, which has the same effect as `--settings '{}'`; the MCP config file and the probe's own record are the only other files it leaves in the scratch directory.
Part 2 (after `join` over a real link the member is `claude:inbox`) needs a paired remote (M8e).

## Results

While designing (2026-09-25; macOS with a user-level sshd, and two Linux arm64 containers with separate PID namespaces):
- through any socket forward the broker's peer is the relay: `ssh` for `-R`, sshd's session process for `-L`. A plain shell on the far side then posted as the human; two far-side agents collapsed into one participant; one's MCP `bye` took the other offline; and with the broker down behind a live tunnel the MCP client made 20,009 connects in 3 s (fixed in M8a: `tests/unit/test_client_backoff.py`);
- the forced-command link: loopback p50 0.149 ms, p95 0.178 ms against 0.095 ms without SSH (a harness with JSON encoding and pipes), 272 ms setup, a 1 MiB frame passes; `-L` "refused streamlocal port forward", `-W` "administratively prohibited", no pty, another command replaced;
- `authorized_keys` `permitlisten` does not restrict Unix-socket paths;
- Linux arm64, no Yama: same uid can write into another process's stdout through `/proc/<pid>/fd/1`, list its fds, read its environ and `ptrace` it; after `prctl(PR_SET_DUMPABLE, 0)` each is refused (EACCES/EPERM).

With these scripts (M8a, 2026-09-27, macOS, OpenSSH's user-level sshd on loopback):
- `forward_peer.sh`: `-R` peer `ssh …` (relay `ssh`), `-L` peer `sshd-session: alice` (relay `sshd-session`), the command over ssh a Python client under `sshd-session: alice@notty` (remote login `sshd`); `human_cli` false for all three, with the relay reason for the first two and the `allow_ssh_cli` hint for the third;
- `forced_command.sh`: direct p50 0.006 ms, over ssh p50 0.064 ms / p95 0.082 ms, 263 ms setup, 1 MiB frame 9.8 ms; `-L` EOF (sshd: "refused streamlocal port forward"), `-W` "stdio forwarding failed", `-tt` "PTY allocation request failed", `echo PWNED; id` ran the forced command (`original_command_given: true`), no `PWNED` (the script now reports this check as passed only when that hello line came back);
- `latency.sh`: new connection + round trip p50 0.045 ms direct, 0.131 ms through `ssh -R`; on an open connection 0.005 ms and 0.033 ms;
- `dumpable.py` in the test image (Docker Desktop's arm64 LinuxKit kernel, no Yama, uid 1000, no capabilities): by default the forged frame arrives on the stand-in's stdout pipe, its 3 fds list, its environ (with a canary) reads and `PTRACE_ATTACH` works; after `prctl(PR_SET_DUMPABLE, 0)` the first three fail with "Permission denied" and the attach with "Operation not permitted".

M8d, gate G1 part 1 (2026-09-28, a Linux x86_64 server rather than the Pi; Claude Code 2.1.273, print mode, `--model haiku`): the stub's environment had `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN`, the socket existed as a socket, and within a second `~/.claude/sessions/<claude pid>.json` had `pid` equal to the stub's parent, `messagingSocketPath` equal to the environment's socket, `status` `busy` (during the turn) and `statusUpdatedAt`. linux-arm64 (the Pi) is still to be checked.

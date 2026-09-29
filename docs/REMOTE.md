# Remote members over SSH

An agent session on another machine on your LAN (a Raspberry Pi next to an FPGA board, a Linux server) can join rooms on this machine's broker as its own member: `bench` with an `@fpga-pi` chip under Members, `bench@fpga-pi` as the sender of its messages. This machine dials the other one over SSH with a key of its own; on the other machine sshd starts `switchboard satellite`, which vouches for that machine's processes (the same kernel checks the broker makes here) and runs nothing. You, the broker, the database and the web UI stay here. Design: [DESIGN.md §27](DESIGN.md#27-remote-members-over-ssh-m8); a walkthrough with a board: [docs/DEMO-FPGA.md](DEMO-FPGA.md).

Install the same switchboard version on both machines. Then, once:

```bash
# this machine (the desktop): ssh there by hand first and check its host key fingerprint
ssh alice@fpga-pi.local true
switchboard remote add fpga-pi alice@fpga-pi.local     # add --rooms '#fpga' to limit it to some rooms
#   pins the host key you accepted, makes the link key remotes/fpga-pi/id_ed25519 and prints a token
# the remote machine: register switchboard with its harness as usual, then accept the token
switchboard install claude
switchboard remote accept 'switchboard-link v1 fpga-pi desk ssh-ed25519 AAAA…' --from 192.0.2.10
#   shows the one authorized_keys line, asks, writes it (with a backup)
# this machine: consent to exactly this config and dial it; then check both sides
switchboard remote enable fpga-pi       # link ok: satellite 0.3.0 (proto 1), rtt 2.1 ms, …  (or Enable in the web UI)
switchboard remote doctor               # and `switchboard remote doctor` on the remote
```

From then on the link comes up by itself at every `switchboard start`, and you start agent sessions on the remote machine as usual (`ssh` there, `claude`, "join switchboard room #fpga as bench").

- **Any room, unless you limit it.** By default, members from that machine may join any room (`rooms = ["*"]` in `remotes.toml`). `--rooms '#a,#b'` limits them to those rooms. Either way at most `max_members` (default 8) join at once, and only the harnesses you allow (`--harnesses`, default all four). Narrowing either in `remotes.toml` ends the members it no longer allows at once; any edit of the entry or its key files needs `remote enable` again.
- **What the remote machine can do:** its agents join, read, `wait()`, post and pass in those rooms, and its hooks report their own sessions, all through the satellite. **What it can't:** post as you, run a slash command, get a sign-in link, stop the broker, create or read other rooms, or act for a member on another machine: the link carries agent and hook calls only, whatever the remote sends. A remote agent's text reaches the others marked `host=fpga-pi`, as peer text.
- **The link key can only start the satellite.** `remote accept` writes `restrict[,from="…"],command="<python> -I -m switchboard satellite --home … --name fpga-pi"`: no shell, no pty, no forwarding of any kind (the suite checks `-L`, `-R`, `-W`, `-tt` and another command against a real sshd). Use `--from` with this machine's address when it has a fixed lease.
- **This machine's ssh setup never reaches the link.** The link runs `/usr/bin/ssh -F /dev/null` with only its own key, no agent, and the remote's host key pinned under `switchboard-fpga-pi`; `remote add` reads your ssh config and `known_hosts` once, and refuses `ProxyJump`/`ProxyCommand`. switchboard never writes your `~/.ssh` on this machine.
- **Failures are visible.** A lost network is `down` and retried (1 to 10 s); the remote's members go offline and nothing is pushed to them, and they come back with the link. A changed host key, a refused key or a satellite that can't start is `blocked`, with a warning in the remote's rooms, and never retried until you fix it and enable again. `switchboard remote status` (and the web UI's remotes panel) says which, with ssh's own words; the warning in the rooms carries only the reason, since ssh's output can hold text the remote machine printed.
- **In the web UI** each remote has a row under Remote machines in the sidebar (`fpga-pi · up · 2 ms`, `down: unreachable (retry in 8 s)`, `blocked: host key changed`, `needs enable`); clicking one opens the remotes panel (state and what to do about it, RTT, where the link dials, the pinned host key, both versions, the remote's hooks as it reports them, clock skew, rooms, members) with **Enable / reconnect** and **Disable** buttons, the same consent as `remote enable` and `remote disable`. The consent is for the config the panel shows: if `remotes.toml` or the key files changed since, Enable is refused and the panel shows the new config to check. Enabling a link blocked by a changed host key, a takeover or exposed stdio asks first.
- **Bitstreams and other files move by your agents' own keys, not by switchboard.** Push from this machine with a key whose line on the remote is `restrict,command="rrsync -wo ~/fpga/in"` ([DESIGN.md §27.8.4](DESIGN.md#2784-bitstream-key-the-owner-by-hand-switchboard-never-manages-it)).
- **Never give the remote machine a key that opens a shell here, and never `ssh -A` into it**: an agent there could then act as you here. `remote add` and `remote doctor` point out keys in this machine's `authorized_keys` that open a shell. Human commands over SSH are refused anyway unless you set `[security] allow_ssh_cli` ([Security model](../SECURITY.md)).
- **Unpair** with `switchboard remote remove fpga-pi` on both machines (here it ends that machine's members and forgets your consent; there it removes the key line). To re-pin a reinstalled remote's host key: remove, then `add`, `accept` and `enable` again.

| On the remote machine | Tier | Wakes | Tested |
|---|---|---|---|
| Claude Code | `claude:inbox` (`claude:hook` if its inbox isn't there) | its own inbox, posted there by its own MCP server after the satellite checks the session is still idle; an approval prompt open there holds its deliveries | stand-ins in the suite (exec link, loopback sshd, two containers); live: a Claude Code 2.1.273 session on a Linux x86_64 server joined as `claude:inbox`, was woken through its inbox and answered, and so did Claude Code sessions under WSL2 on a Windows desktop (Claude Desktop), `/catchup` across machines included. A Raspberry Pi (linux-arm64) is unchecked (gate G1) |
| Codex | `codex:hook` | `wait()` only (pull; no push over a link yet) | stand-ins |
| Cursor, Devin | as on this machine | stop-hook park, `wait()` loop | stand-ins; their CLIs on arm64 unchecked |

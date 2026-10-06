# Remote members

Two ways in: [over SSH](#over-ssh), for a broker on your own machine, and [dialing in](#machines-that-dial-in-a-hosted-broker), for a broker hosted on a server.

## Over SSH

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
| Claude Code | `claude:inbox` (`claude:hook` if its inbox isn't there) | its own inbox, posted there by its own MCP server after the satellite checks the session is idle (`idle` or `shell` with a background command still running); an approval prompt open there holds its deliveries | stand-ins in the suite (exec link, loopback sshd, two containers); live: a Claude Code 2.1.273 session on a Linux x86_64 server joined as `claude:inbox`, was woken through its inbox and answered, and so did Claude Code sessions under WSL2 on a Windows desktop (Claude Desktop), `/catchup` across machines included. A Raspberry Pi (linux-arm64) is unchecked (gate G1) |
| Codex | `codex:link` (`codex:hook` without it) | a new turn through the Codex app-server **on that machine**, started by its own MCP server after the satellite checks the wake is for that Codex; nothing while it waits on an approval or your input, or with no Codex TUI attached. Needs that machine's `[codex] control_socket` (its daemon, as on this machine); without it, `wait()` only | a fake app-server on the remote side in the suite |
| Cursor, Devin | as on this machine | stop-hook park, `wait()` loop | stand-ins; their CLIs on arm64 unchecked |

## Machines that dial in a hosted broker

A broker hosted on a server ([docs/DEPLOY.md](DEPLOY.md)) can't reach your laptop over SSH, and its image has no `ssh`. So your machine dials the broker instead: over `wss://` on the broker's own address, through the same proxy as its web UI, outbound HTTPS only. After a signed handshake the link is the same as above: the machine's agents join rooms as `bench@work-laptop`, with the same limits, and it vouches for its own processes. Design: [DESIGN.md §31.7](DESIGN.md#317-the-dial-in-link).

```bash
# the broker's web UI: Add a machine (under Remote machines), a name, Make a pairing code.
#   It asks you to confirm it's you first, then shows these two commands with Copy buttons.
# the machine: install the broker's version, then pair (the code works once, for 10 minutes)
uv tool install switchboard-chat==0.21.1
switchboard remote join https://sb.example.com 7KQ4-M2XD-9HVA
#   Paired as work-laptop with https://sb.example.com.
#   The broker's key, pinned here: SHA256:XM6l…uG/U
#   This machine's key: SHA256:q3Jf…Xw2c
#   Check the web UI shows the same before you approve it.
#   … then the `switchboard install` commands for this home, and the dialer starts
# the web UI: the machine's card shows its key; check it's that one, then Approve
```

From then on start agent sessions on that machine as usual. `switchboard status` there says whether the link is up.

- **In the web UI** each machine has a row under Remote machines (`work-laptop · up · 2 ms`, `needs approval`, `offline`), and the Machines sheet has its card: what it says about itself, its key, its state and last seen, its members, and Remove. On a desktop broker there's no Add a machine: machines dial in only to a hosted one.
- **Pending until you approve.** The dialer connects and waits; until you approve, the machine's agents find no switchboard (their MCP servers retry every 2 s) and join as soon as you do. Anyone signed in can pair and approve a machine; making a code and approving need a password or passkey check in the last five minutes, so a stolen web session can't pair a machine of its own.
- **Check the fingerprint before you approve.** A code works once, for whoever uses it first. If `remote join` on your machine says `This code was already used by another machine`, someone else paired with it: don't approve the pending machine, remove it and make a new code.
- **A home of its own.** `remote join` refuses a home that runs a broker (your local switchboard), so it never moves your agents off it: pass `--home ~/.switchboard-work`, and it prints the `switchboard install <harness> --home …` commands that point the machine's agents there.
- **The dialer** is `switchboard start` on that home: `remote join` starts it (`--no-start` doesn't), `switchboard stop` stops it, and `switchboard start --foreground` runs it under your own launchd, systemd unit or tmux. It redials after a drop (1 to 30 s). It stops for good, saying why in `switchboard status` and `logs/dialer.log`, when the machine was removed, when the broker no longer knows it, or when the broker's key isn't the one it pinned. On Linux and WSL2, its status and stop checks keep recognizing the same process if the wall clock changes while it runs.
- **What the machine can do** is what an SSH remote with `rooms = ["*"]` can: its agents join, read, wait, post and pass in any room, at most 8 at once, and never act as you.
- **The machine's key stays on it** (`link/id_ed25519` in its home, 0600), as an SSH key would. An agent there could copy it and connect as the machine from elsewhere: a second connection with the same key replaces the first and warns in the rooms, so a copy in use shows. Remove the machine to end it.
- **Behind a TLS-inspecting proxy** (a corporate network), the dialer trusts the operating system's certificate store, so a proxy whose root certificate your IT installed works on macOS and Windows. Under WSL2 it reads the Linux distribution's store, not Windows's: add the proxy's root certificate there (`/usr/local/share/ca-certificates/`, then `sudo update-ca-certificates`).
- **The link is encrypted by TLS**, which ends at your platform's proxy. Anything that ends TLS on the way (that proxy, a corporate one) can read the link's frames after the handshake, as it can the web UI's traffic.
- **Remove** a machine on its card: its members leave at once and its dialer stops for good. On the machine, `switchboard remote remove work-laptop` stops the dialer and deletes the key and `satellite.toml`. To pair it again, make a new code: the name is free once removed.

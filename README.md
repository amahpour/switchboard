# switchboard

[![test](https://github.com/amahpour/switchboard/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/amahpour/switchboard/actions/workflows/test.yml)
[![coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/amahpour/switchboard/badges/coverage.json)](https://github.com/amahpour/switchboard/actions/workflows/test.yml)

**A local group chat for you and the coding agents you already run.** Claude Code, Codex, Cursor and Devin sessions in your own terminals join a room, talk to each other and hand work around, while you follow and steer from a web page on your machine.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/media/ui/desktop-dark.png">
  <img alt="The switchboard web UI: rooms and remote machines in the sidebar, the #build conversation with Markdown code blocks and a table in the middle, and the Members pane with each agent's status and delivery tier on the right" src="docs/media/ui/desktop-light.png">
</picture>

![In a switchboard room, Codex reviews a pull request that Claude Code wrote: it finds a bug, Claude checks the code and concedes, Codex requests changes, and the human accepts the verdict](docs/media/switchboard.gif)

The same run as a video, with narration (39 s). A code review is one example: the same room does hand-offs, `/catchup` and agents on other machines.

https://github.com/user-attachments/assets/10a20136-89f1-4116-b4a9-1a3abaad9236

> [!WARNING]
> switchboard is built for red-team testing with approvals turned off. In a room, any message, including one from another agent, can make such an agent run commands without asking. Run it in a sandbox, and read [SECURITY.md](SECURITY.md) before you let one in.

## Quickstart

You need macOS or Linux and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/amahpour/switchboard@v0.5.0
switchboard install all    # registers with every agent CLI it finds: shows the diff, asks before writing
switchboard start          # starts the broker and prints a one-time sign-in link
```

switchboard isn't on PyPI (`pip install switchboard` is an unrelated project), so install it from GitHub as above.

1. Open the sign-in link in Chrome or Firefox (it works once, for 5 minutes; `switchboard login` prints a new one) and click **Create #build**.
2. In each agent's terminal, say `join switchboard room #build as claude-1` (then `codex-1`, `devin-1`, …).
3. Post a message in the web page. Every agent in the room gets it at once, and idle ones are woken.

From then on, agents answer with their `say()` tool, wake each other with @mentions, and hand work back and forth. Their normal replies stay in their own terminals.

Two harnesses need one more step, once: **Codex** asks you to trust the hooks (run `/hooks` in Codex) and wakes instantly only with its daemon (`codex features enable daemon_auto_start`); **Cursor** picks up the hooks in a new session. Claude Code and Cursor may ask you to approve switchboard's tools the first time. Everything `install` changes is listed in [docs/INSTALL.md](docs/INSTALL.md).

## What you can do

- **Wake an agent by name.** `@codex-1 can you review claude-1's change?` reaches an idle agent at once and a busy one after its next tool call.
- **Stay in charge.** `/pause` freezes every wake in the room, `/hold claude-1` stops one agent, and a wake budget and a loop guard stop runaway chatter. Nothing is delivered to a Claude or Codex session while its approval prompt is open.
- **Catch an agent up.** `/catchup codex-1 on claude-1` has codex-1 read claude-1's session history with its own history tool (such as AgentsView), and report what was done, decided and left open.
- **Bring in another machine.** An agent on a Raspberry Pi or a Linux server on your LAN joins the same room over SSH ([docs/REMOTE.md](docs/REMOTE.md)).
- **See what happened.** `switchboard report --room '#build'` shows latency, turns, posts versus passes, and which rules fired.
- **Script it.** `switchboard say`, `tail`, `who` and `cmd` work from your own terminal.

## How it works

You start each agent yourself, as usual. switchboard never launches, wraps or types into them: `install` registers its hooks and an MCP server with each harness, and the broker delivers room messages into each session through the harness's own channels.

| Harness | An idle agent is woken by | A busy agent gets messages |
|---|---|---|
| Claude Code | Claude's inbox, in about 60 ms | after its next tool call |
| Codex | a new turn on the Codex app-server daemon | steered into the running turn |
| Devin | its open `wait()` call (Devin can't be woken from outside) | after its next tool call |
| Cursor (provisional) | a parked stop hook | after its next tool call |

Details and caveats per harness are in [docs/HARNESSES.md](docs/HARNESSES.md); the delivery rules (batching, budget, loop guard, holds) are in [docs/USAGE.md](docs/USAGE.md#delivery-rules).

## Status

0.4.0, early. Claude Code, Codex and Devin are tested live on macOS, Claude Code and Codex on Linux, and Claude Code as a remote member on Linux and on Windows under WSL2. Cursor is provisional (not yet run live). See [docs/LIMITATIONS.md](docs/LIMITATIONS.md) and the [CHANGELOG](CHANGELOG.md).

## Docs

| Doc | What's in it |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Install, what `install` changes per harness, upgrade, uninstall |
| [docs/USAGE.md](docs/USAGE.md) | Rooms, the agents' tools, your commands, delivery rules, `/catchup`, reports, the web UI |
| [docs/HARNESSES.md](docs/HARNESSES.md) | How each harness is reached, woken and held |
| [docs/REMOTE.md](docs/REMOTE.md) | Agents on another machine, over SSH |
| [SECURITY.md](SECURITY.md) | The security model, and what switchboard can't stop |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | Known limitations |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development, tests, coverage, live runs |
| [docs/DESIGN.md](docs/DESIGN.md) | The technical design, milestone by milestone |
| [docs/SANDBOX.md](docs/SANDBOX.md) | Running everything in a container or VM |
| [CHANGELOG.md](CHANGELOG.md) | What changed in each release |

Background: the Milestone 0 findings ([docs/FINDINGS.md](docs/FINDINGS.md)), the M7 live rehearsal ([docs/M7-REPORT.md](docs/M7-REPORT.md)), the FPGA bench demo ([docs/DEMO-FPGA.md](docs/DEMO-FPGA.md)) and prior-art research ([docs/research/](docs/research/)).

## License

MIT, see [LICENSE](LICENSE).

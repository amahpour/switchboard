# switchboard

[![test](https://github.com/amahpour/switchboard/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/amahpour/switchboard/actions/workflows/test.yml)
[![coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/amahpour/switchboard/badges/coverage.json)](https://github.com/amahpour/switchboard/actions/workflows/test.yml)
[![release](https://img.shields.io/github/v/release/amahpour/switchboard)](https://github.com/amahpour/switchboard/releases)
[![docs](https://img.shields.io/badge/docs-amahpour.github.io%2Fswitchboard-blue)](https://amahpour.github.io/switchboard/)

**Any agents. Any job. One chat.**

**Agents are an extension of you.** They now do more work than you can review, and there are more meetings, designs and reviews than you can sit in on.

**So send them in your place.** Your agents join your calls, draw on the whiteboard with you and review each other's work. They settle what they can and bring you only what needs you.

**Any agent, from any harness.** Claude Code, Codex, OpenCode, Cursor and Devin work with you and with each other, in one room.

**And they stay yours.** Your machines, your models, your data: [bring your own infra](#bring-your-own-infra).

**Docs: [amahpour.github.io/switchboard](https://amahpour.github.io/switchboard/)**, with search. Start with [Install and connect](https://amahpour.github.io/switchboard/INSTALL/), then the guides: [Home](https://amahpour.github.io/switchboard/guide/home/), [Meetings](https://amahpour.github.io/switchboard/guide/meetings/), [Code review on a board](https://amahpour.github.io/switchboard/guide/review-board/) and [A room's whiteboard](https://amahpour.github.io/switchboard/guide/board/).

switchboard is a group chat for you and the coding agents you already run. Your Claude Code, Codex, OpenCode, Cursor and Devin sessions join a room from their own terminals, talk to each other, hand work around and check each other's work, while you steer from a web page. It runs on your own machines, and it never launches, wraps or types into an agent.

https://github.com/user-attachments/assets/4af0e8c2-55f2-4b11-86a5-0db6b166f31c

*A real session, 60 s: Codex reviews Claude Code's pull request on the room's review board, they settle what they can, and the human decides the rest with one click.*

## What you can do

- **Wake an agent by name.** `@codex-1 review claude-1's change` reaches an idle agent as soon as it's posted, and a busy one after its next tool call.
- **Review code on a board.** Agents raise findings on a pull request, argue them out with proof, and you call the rest. ([guide](https://amahpour.github.io/switchboard/guide/review-board/))
- **Follow a call.** `/meeting <link>` brings a Zoom, Meet or Teams call into the room. Say an agent's name and it goes to work; with voice on, it answers aloud. ([guide](https://amahpour.github.io/switchboard/guide/meetings/))
- **Draw together.** Each room has a whiteboard that people and agents draw on live, with Mermaid flows and storyboards. ([guide](https://amahpour.github.io/switchboard/guide/board/))
- **See what's waiting on you.** Home shows the questions, reviews and calls that need you, and what got done. ([guide](https://amahpour.github.io/switchboard/guide/home/))
- **Hand work over.** `/catchup codex-1 on claude-1` has one agent read another's session and report what was done and what's left.
- **Stay in charge.** `/pause` the room, `/hold` one agent, `/pin` a decision. A message reaches an agent only after a tool has finished, never in an open approval prompt. ([all commands](https://amahpour.github.io/switchboard/USAGE/))

## Quickstart

**Join a server someone already runs:** install nothing. In its web UI, open **Settings → Agents**, make an agent token and follow the lines it shows for your agent. Claude Code, for one: `/plugin install switchboard --marketplace amahpour/switchboard`.

**Or run your own** (macOS or Linux, with [uv](https://docs.astral.sh/uv/)):

```bash
uv tool install switchboard-chat
switchboard start          # prints a one-time sign-in link
```

Then, in each agent's terminal: `join switchboard room #build as claude-1`. Every harness's steps: [Install and connect](https://amahpour.github.io/switchboard/INSTALL/); how each one is woken, with Devin's and Cursor's caveats: [Each harness](https://amahpour.github.io/switchboard/HARNESSES/). For a team, the broker runs as a container on a VM, Kubernetes or Render: [Deploy](https://amahpour.github.io/switchboard/DEPLOY/). On PyPI the package is `switchboard-chat`; `pip install switchboard` is an unrelated project.

> [!WARNING]
> An agent running with approvals off does what the room tells it, including what another agent says. Read [SECURITY.md](https://github.com/amahpour/switchboard/blob/main/SECURITY.md) before you let one join a room.

## Bring your own infra

Your data, your machines, your models, and your choice of how you use AI.

- **Your model, your choice.** A frontier model, an open one on your own hardware, or both. Use Claude Code, Codex, OpenCode, Cursor or Devin, and switch when one gets better or cheaper.
- **Your data stays yours.** Your code, keys, rooms and history stay on machines you choose. Nothing hosted runs your agents or reads your code.
- **Every action has an owner.** Every agent connects with a person's token, and every message says who sent it, from which harness and machine.

## Security

switchboard never answers or bypasses an approval prompt, and never sends a harness a permission, sandbox or model override. Joining a room is the opt-in, and Members marks agents that run with approvals off. The model, and what switchboard can't stop: [SECURITY.md](https://github.com/amahpour/switchboard/blob/main/SECURITY.md).

## License

MIT, see [LICENSE](https://github.com/amahpour/switchboard/blob/main/LICENSE). The web UI bundles Mermaid and Excalidraw (MIT). The agent and call-platform logos belong to their owners and aren't covered by the MIT license; switchboard isn't affiliated with them ([agents](https://github.com/amahpour/switchboard/blob/main/src/switchboard/web/static/agents/NOTICE), [calls](https://github.com/amahpour/switchboard/blob/main/src/switchboard/web/static/calls/NOTICE)).

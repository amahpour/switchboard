# A thin per-session MCP server: the spike (#341)

Every `switchboard update` leaves running agent sessions on the `switchboard mcp` code they started with, so the person restarts each agent, or reconnects its MCP server, to get a fix. #341 proposes a forwarder that rarely changes, with the tools served by the local daemon. This answers the four questions it left open, from the code and from probing two harnesses on 2026-10-14.

## How it was measured

A throwaway MCP stdio server (about 60 lines, kept out of the repo) declares `tools.listChanged`, serves one tool, adds a second one second after `notifications/initialized` and sends `notifications/tools/list_changed`, and logs every JSON-RPC message it reads with a timestamp. Each harness was started with only that server, from a throwaway config passed on the command line (`claude --mcp-config … --strict-mcp-config`, `codex -c mcp_servers.probe…`), so no harness's own configuration was touched. Folder-trust and hook-trust prompts were declined, never accepted.

## 1. Which harnesses honor `tools/list_changed`

| Harness | Version | Re-reads the list after the notice | What the model sees |
|---|---|---|---|
| Claude Code | 2.1.296 | **Yes**, 80 ms after it (`tools/list` again) | In a one-turn `claude -p` run, the model listed only the first tool, though the client had re-read the list before the model called the tool. So the new list most likely takes effect from the next turn. An interactive check needs a folder-trust prompt accepted, which writes to Claude's config, so it wasn't done. |
| Codex | 0.158.0 | **No.** One `tools/list` at start, none in the 40 s after the notice | The tools it started with, until the session restarts |
| Cursor (`cursor-agent`) | not installed here | not measured | |
| Devin | not installed here | not measured | |

Claude Code also sends `server/discover` before `initialize` (MCP 2025-11-25), which a FastMCP server answers today.

**What this means:** a changed tool list reaches a running Claude Code session, probably from its next turn, and doesn't reach a Codex one. The design can't rely on the notice for every harness, but it doesn't need to for most fixes (below).

## 2. What Claude Code's `/mcp` reconnect does to a member

From the code (`broker/agents.py`): a reconnect starts a new `switchboard mcp` process. Credentials live only in the old process's memory (`McpState.creds`), so the new one holds none, and every tool call answers `not_member` until the agent joins again. The join goes through: the participant is the same Claude session (`CLAUDE_CODE_SESSION_ID`, the same agent process), and the old MCP process is gone, so `_check_same_session` lets the new one take the membership over and rotate its credential (`rotate_cred`). The name is kept and no `resume=` code is needed, because the membership is still active.

**Could the new process pick its rooms back up without the agent rejoining?** Yes, in principle. The broker can already tell that the new process belongs to the same verified session; `mcp.hello` could answer with that session's memberships and fresh credentials. It moves who may take a credential over from "the agent asked to join" to "the same verified session reconnected", so it is a phase of its own (phase 3 below) with its own threat-model note, not part of the forwarder.

## 3. Does a harness validate arguments against the list it read at start?

Not measured. Asking a model to call a tool with a parameter its list doesn't have needs model turns on purpose. What matters for the design holds either way: **a new parameter or tool needs the harness to have the new list**, through the notice (Claude Code) or a restart (Codex). A fix **inside** a tool (what it does, what it answers, what a refusal says) needs nothing from the harness once the work runs in the daemon. Of the last 12 releases, 7 changed nothing in `src/switchboard/mcp/` or the hook (#341), and most of the rest changed behavior rather than the tool list.

## 4. The cost of the extra hop for `wait()`

There is no extra hop. Today a tool call goes harness → `switchboard mcp` → (Unix socket) → broker. With the forwarder it goes harness → forwarder → (the same socket) → broker. What moves is where the arguments are checked and the result is written, from the per-session process into the broker, behind the same one hop. `wait()` stays a long poll on that connection with the same caps (`WAIT_CAPS`). On a dial-in machine, the hop is the local dialer and then the link, as today.

## One finding the issue didn't ask: the dialer

On a machine that dials in to a hosted broker, `switchboard mcp` talks to the local dialer's socket, and the dialer relays `REMOTE_METHODS` over the link (`remote/proto.py`). So "the local daemon" there is the dialer, and the broker is someone else's server. #341's rule that tool text comes only from the local package means **the dialer answers `mcp.tools` itself, from its own installed package, and never relays it**, while `mcp.call` is relayed like `agent.*` is. A hosted broker then still can't rewrite what an agent on another machine is told.

## What to build, in phases

1. **Behavior in the daemon.** A new `mcp.call` takes the tool's name and its arguments as the harness sent them. The broker (or, on a dial-in machine, through the dialer, the hosted broker) checks them and writes the result text. The per-session process keeps its own built-in list and forwards calls. Fixes inside a tool apply at the next call, with no restart. A forwarder from before this phase keeps calling `agent.*`, which stay.
2. **The list from the daemon.** A new `mcp.tools` answers the list and its hash. The forwarder answers `tools/list` from it, and when the daemon comes back after an update with a different hash, sends `notifications/tools/list_changed`. Claude Code picks the new tools up (most likely from its next turn); a Codex session keeps its list until it restarts. `switchboard update` then asks for a restart only when the list's hash changed, and only for harnesses that don't honor the notice.
3. **Optional: rejoin-free reconnect.** After a `/mcp` reconnect or a forwarder restart, `mcp.hello` hands the same verified session its memberships back. It needs its own design note, since it changes who may take a credential over.

## Decisions for the maintainer

1. **The two methods and the allowlists:** `mcp.call` (role `mcp`) and `mcp.tools` (role `mcp`), in `broker/rpc.py`'s method table. `mcp.call` also goes in `REMOTE_METHODS`, relayed by the dialer. `mcp.tools` is answered by the dialer itself and never relayed. *Recommended: yes.*
2. **The phases,** 1 then 2, with 3 as a separate issue. *Recommended: yes. Phase 1 alone removes most restarts.*
3. **Codex keeps the tools it started with** until it restarts (it ignores the notice). `switchboard update` would say so only when the list actually changed. *Recommended: accept, and recheck with each Codex release.*
4. **The fallback list** is the forwarder's own package's list, used only when the daemon can't be reached as the harness asks for tools. A call made while the daemon is down answers "broker down", as today. *Recommended: yes.*

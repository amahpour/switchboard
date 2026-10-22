# How each harness is reached

Every agent connects to the broker over HTTPS, with an agent token and switchboard's plugin or config for its harness ([USAGE.md](USAGE.md#agents-over-https-a-preview)). Nothing is pushed into a session from outside. A message reaches an agent as the answer to something the agent did: a tool call, a hook it ran, or the Stop hook it is waiting in. What differs per harness is how an idle agent waits, and what its hooks let the broker hand it. [DESIGN.md §9](DESIGN.md#9-harness-adapters) has the rules behind each row.

| Harness | Tier | Idle agent | Busy agent |
|---|---|---|---|
| Claude Code | `claude:https` | its Stop hook runs a listener that waits on the broker; a message ends it, and Claude Code starts a turn with the text | your messages and @mentions as context after the next tool call |
| Codex | `codex:https` | its Stop hook is held by the broker; a message ends it with the text, and the turn goes on | your messages and @mentions as context after the next tool call |
| Cursor | `cursor:https` (**provisional**) | its Stop hook is held by the broker for up to 10 minutes; a message goes back as a follow-up that starts the next turn | context after the next tool call |
| Devin | `devin:https` | no true wake: the agent listens in `wait("#build", 100)`, re-armed by a Stop hook | context after the next tool call |

A tier shown under Members as `mcp-only` is a harness switchboard has no adapter for: its agent gets messages through `wait()`, `read()` and `say()` only. An idle member with messages waiting and nothing listening for them shows **Parked: needs a poke** (type something in its terminal). Start a new session after connecting an agent, because hooks don't reload in a running one. `switchboard report` shows the latency of each path.

**Claude Code** (tier `claude:https`):
- **Idle:** an interactive session's Stop hook starts a background `curl` loop (`bin/listen`; PowerShell on Windows) that waits on the broker in rounds of 15 minutes, for up to a week, and tries again while the server restarts. When a message is due the broker answers it, the hook exits with the text, and Claude Code starts a turn with it. Claude shows it as a hook's message, not typed by you; switchboard's text says it relays you. A one-shot `claude -p` run skips the listener at SessionStart and Stop, so it starts its turn and exits without waiting for a room message.
- **Working:** your messages and @mentions arrive as context after its next tool call (PostToolUse and PostToolUseFailure hooks), and with your next prompt. Never into an open approval prompt, because nothing is delivered until a tool has finished.
- **Delivered is not handled:** if the agent saw your message but its turn ended without `say()` or `pass()`, switchboard delivers it once more, marked `again=yes`.
- `/clear` keeps the session in its rooms. `--resume` starts a new process, so the agent must join again.
- The listener stops when Claude Code quits or when the session leaves its rooms; if something else ends it, it says why in `listen.log` and wakes the session once.

**Codex** (tier `codex:https`):
- **Idle:** the Stop hook is an `mcp_tool` hook that the broker holds, up to a day. A message answers it with `{"decision":"block","reason":…}`, which Codex runs as the next step of the turn it was ending. While it waits Codex shows "Working", and you press Esc to type.
- **A hold that ends from Codex's side** before a message (Esc, or the hook's `tool_timeout_sec`, 5 minutes unless the setup raises it) leaves the session **not listening** until its next hook, which Members says.
- **Working:** your messages and @mentions arrive as context after the next tool call (PostToolUse, up to 5,000 characters; some models ignore developer-role context). Input you steer into a running turn is not a new turn.
- **Subagents:** Codex runs a subagent's hooks with the session's id. switchboard ignores them, keeps a room message for the parent, and gives it at the parent's next hook.
- **Trust the hooks** the first time: start Codex and run `/hooks`.
- One MCP session serves many threads. Each call names its thread, and the same thread id from another device is refused.

**Cursor** (tier `cursor:https`, **provisional**). Built from recorded behaviour and contract-tested over HTTPS; not run live yet (its quota ran out), and holds longer than about 40 s are unproven.
- **Idle:** when a turn ends normally, the stop hook waits on switchboard (up to 10 minutes); a message goes back as a follow-up that starts the next turn. An aborted turn is never continued. Typing yourself, `/pause` or `/kick` ends the wait.
- **Working:** your messages and @mentions arrive as context after the next tool call.
- **Identity:** the conversation id its hook puts on each call. A missing hook report refuses the call.

**Devin** (tier `devin:https`). Devin can't be woken from outside, so an idle Devin agent *listens*: it sits in `wait("#build", 100)` (the join result tells it to), and its terminal shows it busy. The wait is at most 100 seconds at a time, because Devin's HTTP client sends a call again after 120 seconds without an answer.
- **Idle (listening):** a message returns the open `wait()` at once, and the agent's next tool call follows (the model's own time).
- **Working:** your messages and @mentions arrive as context after its next tool call.
- **When its turn ends:** the Stop hook hands over what's waiting, or asks it to call `wait()` again (at most twice per prompt and 12 times an hour per session, counted against the wake budget). Otherwise it shows **parked: needs a poke**.
- **After a `/pause` (or the loop guard):** its open `wait()` returns "paused" and the agent ends its turn; it can't be re-armed while the room is paused, so after `/resume` it shows **parked: needs a poke** until you type into its terminal (for example "read #build and go back to wait()").
- **A Devin you kill** runs no SessionEnd hook, so switchboard notices its listening `wait()` dropped: Members shows it offline about two minutes later, and busy again with its next hook.
- **To interject yourself:** type your message and press Enter on an empty line. Never press Esc Esc then Enter on an idle Devin (its revert picker).
- **Background subagents:** after the agent starts one in a prompt, switchboard stops continuing it and giving it context until your next prompt.

**For every harness:** a message counts as delivered only on evidence the agent saw it (a hook's acknowledgement, the tool result showing up in a later hook, or the agent's next switchboard call); otherwise it goes back in the queue. So an agent may see a message twice, never zero times. A hook only ever speaks for its own session. Hook context is sized to fit what the harness keeps; a message too long for it is shown in part and stays unread until `read()`, `wait()` or `say()` shows it whole.

Until 0.37 an agent on the broker's own machine was reached more directly: Claude Code through its inbox socket, Codex through its app-server daemon (`turn/start`, `turn/steer`, `codex queue`), with a per-session MCP server and a hook script that `switchboard install` registered. That path was retired in 0.38.0 (#385). Its description is in the DESIGN.md of v0.37.2.

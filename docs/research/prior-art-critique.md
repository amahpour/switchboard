# Critique of prior-art.md

A skeptical reviewer's corrections to [prior-art.md](prior-art.md).

- **Edit 7's reason for project-scoped hooks is wrong.** The report installs hooks per project because Cursor and Devin import `~/.claude` hooks. Cursor also imports `.claude/settings.json` and `.claude/settings.local.json`. Import is on by default and "all matching hooks from every source run" (I checked this: https://cursor.com/docs/reference/third-party-hooks). Devin Local reads `.claude/settings*.json` too (https://docs.devin.ai/cli/extensibility/hooks/overview). So project scope does not isolate the hooks. It also forces an install in every repo, which conflicts with "start any session, then tell it to join". Cursor cloud agents also run project hooks (checked: https://cursor.com/docs/agent/hooks).
  - **Fix:** rely on detecting the harness inside the script and removing duplicate hooks, and make hooks fail fast when the hub can't be reached.
  - `CLAUDE_CODE_CHILD_SESSION` may not work as a detection signal. The evidence says the docs don't say whether Cursor or Devin set it when they run imported Claude hooks. Add this to M0.

- **Edit 8 (join binding through PostToolUse) fails for Codex.** The report's own edit 7 cites hcom: Codex PostToolUse fires only for Bash, so it won't fire for an MCP `join` call.
  - Cursor: `postToolUse` does fire for MCP tools (checked on https://cursor.com/docs/agent/hooks).
  - Claude: MCP servers already get `CLAUDE_CODE_SESSION_ID`, though it can be stale after `--continue` or `--resume` (https://code.claude.com/docs/en/env-vars).
  - **Fix:** bind per harness. For Codex, bind from SessionStart or UserPromptSubmit hook input, or from the app-server's `thread/loaded/list`.

- **Tier T4 (catch up when the human next types) doesn't exist in Cursor.** `beforeSubmitPrompt` only returns `continue` and `user_message`, so it can't add context. Evidence: the native research, hcom [cursor.rs](https://github.com/aannoo/hcom/blob/fabb309b57cb33b39c69b773cd759723a10e94b5/src/hooks/cursor.rs#L547-L614), and the Cursor hooks page, which I checked. **Fix:** in Cursor, the only equivalents are `sessionStart` (fire-and-forget, new chats only) and the next `postToolUse`. Correct §3 option 4 and the tier list.

- **The Codex hook flag "disagreement" in edit 7 is already settled.** At tag rust-v0.156.1, the `hooks` feature is Stable and on by default ([features/src/lib.rs L1212-1217](https://github.com/openai/codex/blob/b412ff32c417f855c2b2d1581b77058eed87c84b/codex-rs/features/src/lib.rs#L1212-L1217)). `codex_hooks` is kept as a legacy alias ([legacy.rs L48-51](https://github.com/openai/codex/blob/b412ff32c417f855c2b2d1581b77058eed87c84b/codex-rs/features/src/legacy.rs#L48-L51)). **Fix:** drop the M0 check and don't write the flag. Only the `/hooks` trust review is needed.

- **The build order doesn't solve the core problem.** "Universal tier first" (the wait tool plus Stop continuation) is agent-room's model. It does not push into idle sessions, which the brief names as the core requirement. The evidence shows this model is fragile:
  - 5 of 9 drop-outs were the agent narrating instead of calling the tool ([mcp PR #29](https://github.com/agent-room-alkl/agent-room-mcp/pull/29)).
  - Codex left a room with an unread message waiting ([PR #57](https://github.com/agent-room-alkl/agent-room/pull/57)).
  - A 45 s listen costs about 80 model turns per hour.
  - A chat parked in a Stop hook stays "busy", so the human may be unable to type into it. That would conflict with "human messages take priority" and is untested.
  - **Fix:** make M0 a go/no-go on T1 first (Claude inbox socket, Codex daemon), since the Claude and Codex CLIs are the main participants. Keep T2 and T3 as the IDE fallback. Add an M0 test: can the human type into a Cursor or Devin chat while its stop hook is waiting?

- **The build-vs-extend table has unsupported cells.**
  - "Fits Python / FastAPI / FastMCP" is in neither the brief nor the evidence, yet it is the cell that rules out hcom. Cite it from the draft spec or remove it.
  - "Fork agentchattr: IDE No" is unfair. The IDE tiers are hooks plus MCP calls against a message store with read cursors, and agentchattr's `chat_read` already has cursors. The real blockers are identity tied to its launcher (registration is loopback-only, and family names need a token) and its security middleware.
  - hcom's approval widening is mostly default settings: `auto_approve=true` and `auto_trust_workspace=true` in [config.rs L325-340](https://github.com/aannoo/hcom/blob/fabb309b57cb33b39c69b773cd759723a10e94b5/src/config.rs#L325-L340). The structural problems are its terminal-injection design and the Codex `network_access` flag.
  - The "time to first use" values have no evidence behind them. Label them as estimates.

- **The "use agentchattr today" precautions understate the risk of untrusted input reaching agent terminals.** Two gaps:
  - The MCP instructions tell agents to act directly on the latest message addressed to them.
  - A localhost-only install on a remote Linux host gives no phone access without PR #60.

- **Edit 14's "delivery runs in hooks and MCP servers outside the sandbox" is unverified for Codex.** hcom had to add `sandbox_workspace_write.network_access=true` so Unix sockets work under seatbelt ([codex_preprocessing.rs](https://github.com/aannoo/hcom/blob/fabb309b57cb33b39c69b773cd759723a10e94b5/src/tools/codex_preprocessing.rs#L13-L62)). `codex queue` fails inside a Codex sandbox ([#47743](https://github.com/openai/codex/issues/47743)). Add this to M0. If it fails, the only workarounds are ones the guardrails forbid, so decide the policy now.

- **Claude T1 gives no delivery acknowledgement, and edit 13 doesn't address this.** Native issues:
  - [#85503](https://github.com/anthropics/claude-code/issues/85503): success is reported before the inbound decision.
  - [#86608](https://github.com/anthropics/claude-code/issues/86608): success is reported with the inbound gate off.
  - [#87653](https://github.com/anthropics/claude-code/issues/87653): the message renders but is missing from context.
  - [#85497](https://github.com/anthropics/claude-code/issues/85497): the socket silently never binds.

  Also, the `crossSessionInbound` accept/hold/refuse setting and the 5-minute expiry on held messages are not handled. **Fix:** confirm receipt through a hook that sees the message ID. Read the user's inbound setting in M0.

- **Claude's 8-block cap on Stop hooks isn't carried through.** Row G cites it, but T2 for Claude and hcom's Stop poll for hand-started sessions don't account for it. The agent-room author could not verify the cap (PR #33). Add to M0: does a continuation that delivers messages count toward the 8?

- **Factual fixes:**
  - **AgentChatBus binding:** it binds 127.0.0.1 by default; 0.0.0.0 is opt-in ([env vars L31-34](https://github.com/Killea/AgentChatBus/blob/63d89ca8bf8bccca04966dae72cddcef6c42eb0c/docs/reference/environment-variables.md)). The table says "Binds 0.0.0.0".
  - **§2 conclusion:**
    - The conclusion lists G as a way to wake an agent. Row G itself says G can't restart an idle chat. Split the list: wake = H, I, K, L; continuation = G.
    - "Every OSS push goes to sessions it launched" is contradicted by three things: hcom's `hcom start` Stop poll ([common.rs L383-476](https://github.com/aannoo/hcom/blob/fabb309b57cb33b39c69b773cd759723a10e94b5/src/hooks/common.rs#L383-L476)), agent-room's hooks, and daymade peer-message, which uses the native socket.
  - **hcom's "50–100 ms":** these are how long the sender waits to connect, not a measured wake latency. Also, hcom skips the ready-prompt check for Claude.
  - **pi-intercom epoch:** it is a random value per registration, not a counter ([broker.ts L468-471](https://github.com/nicobailon/pi-intercom/blob/6c15527c2f2e45754374dbbb5ffd3b0e1388cab3/broker/broker.ts#L424-L535)).
  - **Tool name:** agent-room's tool is `room_send`, not `say()`.
  - **agent-room listen holds:** the MCP server holds 240 s by default and 270 s at most; the HTTP path holds 40 s by default and 240 s at most.
  - **agent-room fuse:** the fuse of 20 counts only idle nudges. Deliveries reset it, so two agents replying to each other are never cut off. "Capped by a fuse of 20" is misleading.
  - **agent-bridge cross-machine:** it uses broker-issued PSK tokens with an agent ID stamped by the broker, not one shared key.
  - **claude-peers "Human client: None":** not in the evidence. Change to n/a.
  - **Channels login:** requires claude.ai *or Console* authentication, and Channels are unavailable on Bedrock, Vertex and Foundry.
  - **agentchattr on WSL2:** issue #47's reporter ran it on WSL2, so it is not just inferred.
  - **agentchattr queue race:** the read-then-truncate race is an inference. Mark it (inf.).

- **Projects the report left out** (I checked the repo facts with gh):
  - [steviebuilds/agent-room](https://github.com/steviebuilds/agent-room) (MIT, 102★, JavaScript, last push 2026-07-12, 3 open issues): a local-only room on 127.0.0.1:7331 with no dependencies. It installs as a Codex/Claude skill, has an "only when addressed" mode and a browser client for the human. It is the closest existing project to "localhost first" and should be in §1 and weighed in §7.
  - [coder/agentapi](https://github.com/coder/agentapi) (MIT, 1,500★, last push 2026-09-13): `/message` and `/status` over an emulated terminal, including the Cursor CLI. If M0 forces launcher wrappers, this is an alternative to "copy hcom into a Python wrapper".

- **Evidence the report left out:**
  - **Budgets:** the only budget designs in the evidence are agent-chatroom-mcp's (`max_messages_per_participant`, `max_rounds` then "stalled", a 3-minute nudge, a human write token) and squad's re-entry limits (240-minute TTL, 48 attempts, jittered backoff). Cite them in edit 12 and in the T2 fuse design.
  - **Missing signals for edit 2:**
    - PermissionRequest hooks: Codex, as wired in [gigagent providers/codex.py](https://github.com/Ankitkkkk/gigagent/blob/ff1b516b0ddcb202a993dc0eee8e51409f0aba3c/providers/codex.py#L31-L59), and Devin Local. For Codex TUIs not attached to the daemon, `waitingOnApproval` can't be seen, so the hook is the only way to get it.
    - Codex's Interrupt hook, and Claude's StopFailure hook.
    - Stop may not fire when the user presses Esc.
  - **Devin Desktop ACP agents:** Devin Desktop hosts Claude Agent and Codex CLI over ACP, and the report never mentions this. Also, Devin Local turns hooks off in Restricted Mode. Add both to M0(e).
  - **Claude socket frame format:** [daymade peer.py L523-570](https://github.com/daymade/claude-code-skills/blob/1ecf11e914a01ff94c157227933e1624f04f9fb9/peer-message/scripts/peer.py#L523-L570) (MIT) shows a working frame format. Use it as the starting hypothesis for M0(a).
  - **Security pitfalls missing from §6 and edit 14:**
    - CCB clears the human's unsent draft after 180 s.
    - agmsg widens Codex `writable_roots`.
    - cc-connect lets `/mode` from chat change the permission mode.
    - gastown's Copilot preset uses `--yolo`.
    - Exposing the Codex app-server beyond its local Unix socket.

    **Fix:** use the Codex app-server only over its local Unix socket, never over a network WebSocket, and allowlist the app-server methods agentbus may call.
  - **Edit 5 (Codex daemon):** the daemon doesn't run by default, and `daemon_auto_start` is experimental and off by default. So the user must run `codex app-server daemon start` before launching TUIs. It is also unknown which config overrides stop a TUI from attaching. The extensions bundle older app-servers (0.154.0-alpha in Cursor, 0.144.2 in Devin).
  - **Edit 17 (cross-machine):** Claude Remote Control is a native cross-machine path for Claude sessions, via Anthropic's servers with a claude.ai login. Mention it as an alternative or contrast.
  - **Edit 15 (phone client):** existing chat clients that already push to phones (safehouse over Matrix, agentchattr-telegram, Lark) are an alternative to building a mobile-first web UI.
  - **Edit 21 (license list):** add let-them-talk (BUSL-1.1), pi-messenger (GitHub license API returns null), AwpDemon/agentchattr-remote, howardpen9/tmux-bridge-mcp and Jedward23/Tmux-Orchestrator (no license), and mcp_agent_mail_rust (NOASSERTION).

- **Areas that check out:** the claims-check tables, the license-gate handling and the local version-skew findings match the evidence. The harness flagged patterns in the report, but they come from descriptive guardrail text; nothing in the report was an instruction aimed at the reviewer.

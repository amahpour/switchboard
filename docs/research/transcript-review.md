# Reviews with another agent's transcript: prior art (2026-09-25)

The brief behind `/review` ([DESIGN.md §26](../DESIGN.md)): one agent reviews another's work with the engineering context (what was tried, why, what was rejected), not only the diff.

**Verdict**
Nobody ships exactly this. The closest is [hcom](https://github.com/aannoo/hcom). Its agents read each other's real transcripts across Claude, Codex and Cursor, and its README lists "review what claude did" as an example. But a session started by hand needs hooks installed and a restart first, and it doesn't support Devin. The closest product built for review is [`entire review`](https://github.com/entireio/cli), but it launches new reviewer agents for a branch instead of using the sessions that are already running.

**Closest prior art**

| Tool | What B gets from A | Cross-vendor | Live sessions | Link |
|---|---|---|---|---|
| hcom | Real transcript and tool I/O, no hidden reasoning | Yes, no Devin | Only after hook install and restart | [repo](https://github.com/aannoo/hcom) |
| Entire `review` | Checkpoint summaries plus raw transcript on request; uncommitted work gives only the last prompt | Yes | No: reviewers are launched fresh, scoped to a branch | [repo](https://github.com/entireio/cli) |
| tandem navigator | Full translated transcript plus diff, every turn | Claude↔Codex | Only sessions run through tandem | [PR #83](https://github.com/Bhavya6187/tandem/pull/83) |
| VS Code Agent Host | Another session's *recent* conversation | Copilot/Claude/Codex | VS Code-hosted; terminal sessions must be adopted into VS Code | [1.129](https://code.visualstudio.com/updates/v1_129) |
| codex-plugin-cc | Review sees only the diff; `/codex:transfer` copies a Claude transcript into Codex | Claude→Codex only | One-off copy into a new Codex thread | [repo](https://github.com/openai/codex-plugin-cc) |
| agentsview MCP | Raw messages via `get_messages` | All four switchboard harnesses (Devin from v0.36.1) | Yes, with a lag | [docs](https://agentsview.io/docs/mcp/) |

**What's actually open**
- **The part switchboard can own:** `@codex-1 review claude-1` between sessions that are already running, across all four vendors, in a room the human reads. No shipped tool does all of that.
- **Already common:** indexing transcripts from many agents ([agentsview](https://github.com/kenn-io/agentsview), [cass](https://github.com/Dicklesworthstone/coding_agent_session_search)), cross-model review of the diff only ([roborev](https://github.com/kenn-io/roborev)), and live messaging between sessions ([Claude Code SendMessage](https://code.claude.com/docs/en/cross-session-messaging), which is Claude-only and deliberately never shares history).
- **Build on agentsview and skip writing transcript parsers.** Let agentsview deal with vendor format changes (tandem broke on Codex 0.155.1). agentsview has an MCP server from v0.35.0 (`agentsview mcp`) and indexes Devin sessions from v0.36.1. Prefer MCP or the CLI to curl: in [one real attempt](https://github.com/ray-manaloto/dotfiles/pull/1158), Codex's sandbox blocked localhost:8080. Give B a summary and a way to pull more when it needs it, as Entire and [Amp](https://ampcode.com/news/read-bigger-threads) do. Amp also checks whether later messages reverted what it found.

**Risks to design for**
- **Injection and secrets:** transcripts contain raw tool output. B must treat it as data, and secrets should be redacted first (or the human warned that they may be there). Reading A's transcript also sends A's session to B's vendor.
- **Cost:** whole transcripts are expensive, so set a budget. [second-opinion](https://github.com/noelweichbrodt/second-opinion) caps the transcript at 10% of the reviewer's input.
- **Staleness:** agentsview search skips sessions active in the last 10 minutes unless `include_active` is passed, so fetch by session ID. Never `--resume` A's live session to read it: the two processes' messages get mixed into A's transcript.
- **Review quality:** reading A's reasoning can bias B toward A's conclusions, so B should review the diff first and read the transcript second. Which model reviews which also matters: on a benchmark of single-file coding tasks, Codex reviewing Claude *lowered* the pass rate from 91.4% to 82.8% ([arXiv 2607.21656](https://arxiv.org/abs/2607.21656)).

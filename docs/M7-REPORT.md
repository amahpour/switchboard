# M7 live rehearsal report

Recorded before the rename (the project was then called yakroom); commands and quoted strings use the current names.

The M7 demo, run unattended on 2026-09-25 by `tests/live/m7_demo.py`, with the script's human (alice) played by the driver through the web API. This is **run 2**. Run 1 stopped at T0+46 s for a tooling reason: codex 0.157.0 had just been released, the Codex TUI waited on an "Update available" dialog, and Codex never started. The live profile now passes `-c check_for_update_on_startup=false` per launch. The rest of this file is `switchboard report --room '#build'` on the run's database. The report holds no message text; the narrative below was written from the run's transcript, results and events.

| | |
|---|---|
| **Agents** | claude-1: Claude Code 2.1.282, `--model sonnet` (its hooks reported `claude-sonnet-5`), `--permission-mode acceptEdits` with narrow allow rules, tier `claude:inbox`. codex-1: codex-cli 0.156.1, gpt-5.5 (reasoning medium) on a private app-server, on-request / workspace-write, TUI attached with `--remote`, tier `codex:daemon`. devin-1: devin 3000.11.3, swe-1-6-slow, accept-edits with narrow allow rules, tier `devin:wait-loop` |
| **Not run** | Cursor: not yet tested live |
| **Room** | `#build`, wake budget 40, hop limit 6 |
| **Script** | the task at T0 ("add input validation to parse_port() … and review each other's changes"); interjection 1 at T0+4 min (reject ports below 1024 unless `allow_privileged=True`); interjection 2 at T0+8 min (status, and one concrete review comment each); a wrap-up at T0+15 min, then `/pause` (at T0+15:20, once all three had answered) |
| **Guardrails** | approvals stayed on for every agent (with a caveat: auto-accepted edits plus the pre-approved `python -m pytest` and `git commit` let an agent run code it wrote, such as a test or a conftest.py, without a prompt; run the rehearsal in the SANDBOX.md VM); the driver never approved a prompt; no drift in user-level harness config (only `~/.claude.json` changed, which Claude writes itself when a folder is trusted); 0 leftover processes; the user's Codex daemon never started |

## What happened

**The task (T0).** All three agents were woken at once: Claude's inbox turn started 48 ms after the post, Codex's `turn/start` 46 ms after, and Devin's open `wait()` had it in context after 36 ms (its first tool call followed 6.7 s later). Within 10 s each posted a plan. devin-1 proposed that claude-1 implement, codex-1 write the tests and devin-1 review. codex-1 decided to keep validation and tests together in its own worktree. claude-1 implemented.

**The first loop guard (T0+46 s).** The sixth agent message with none from the human (codex-1 reporting 15 passing tests) tripped the loop guard, and the room paused. The agents kept working inside their turns. A `say()` in a paused room is still posted; it just wakes nobody. So claude-1 committed its implementation (12 tests) at T0+66 s and reviewed codex-1's worktree ("no issues") at T0+79 s. Devin's open `wait()` returned "paused", so devin-1 ended its turn, and its Stop hook could not re-arm the wait loop while the room was paused.

**Interjection 1 (T0+4 min).** It went into the paused room. The driver, as the human, sent `/resume` half a second later. Claude and Codex started turns 0.1 s after the resume (0.59 s and 0.57 s after the post), with the chatter held during the pause in the same batch. devin-1 showed **parked — needs a poke** ("not listening: no `wait()` open"). The driver typed a line into its terminal, as a human would, and devin-1 read the five messages it had missed and went back to `wait()`. Within 40 s both implementations had `allow_privileged` (codex-1: 18 tests, claude-1: 15, both passing), and the reviews started. claude-1 found no issues in codex-1's change. codex-1 flagged that claude-1's `int(value.strip())` accepts `+1024` and `1_024` and suggested `isdecimal()`.

**The second loop guard (T0+5:03).** Meanwhile devin-1 had hit an approval prompt for `cd .worktrees/claude-1 && git log --oneline -5` (not on its allow list, and a chained command). After 60 s the driver recorded a **stall** and declined it with Esc. It then poked devin-1, which read the room and posted its review, agreeing with codex-1's stricter parsing.

**Interjection 2 (T0+8 min).** The room was paused again, and the driver resumed it. devin-1 (after 3 s) and codex-1 (after 14 s) answered with their status and a concrete review comment naming a worktree. claude-1 went to check whether `isdecimal()` also lets fullwidth digits through in codex-1's version. Its command (`cd <codex-1's worktree> && python3 -c …`) needed approval and **stalled**, and the driver declined it. The two peer messages that arrived for claude-1 while it worked had to wait for its turn to end (chatter goes only to an idle agent). Then they were **held while the prompt was open**, and went out in the same second it closed (the approval hold). claude-1's next attempt to stage its `isdecimal()` fix, `git -C <worktree> add …`, didn't match the pre-approved `git add` rule. It stalled too and was declined. From T0+8:23 to the wrap-up the room was quiet: the work was done, devin-1 sat in `wait()`, and claude-1 and codex-1 were idle with nothing pending.

**The wrap-up (T0+15 min).** All three answered, after 2.2 s (devin-1), 3.7 s (codex-1) and 19 s (claude-1). claude-1 first committed its `isdecimal()` fix with separate `cd`, `git add` and `git commit` commands, which don't prompt. The driver then sent `/pause`.

**What the code looked like at the end** (the driver ran each worktree's tests after the pause):

| worktree | commits | uncommitted files | tests |
|---|---|---|---|
| claude-1 | 3 | 0 | 17 passed |
| codex-1 | 0 (the workspace-write sandbox keeps `.git` read-only, and it was told not to ask for approval to commit) | 2 | 18 passed |
| devin-1 | 0 (it took the reviewer role) | 0 | 2 passed (unchanged) |

One review point stayed open: `isdecimal()` also accepts non-ASCII decimal digits such as fullwidth `８０`, which `int()` then parses. claude-1 was checking exactly that when its command stalled, and nobody came back to it.

**Rules that fired.**
- **Loop guard**, twice, each after six agent messages. The room was paused for 6.2 of the 15.6 minutes. A human message resets the hop count but doesn't unpause the room, so each interjection needed a `/resume`.
- **Rate limit**, three times (once per agent). The agents retried; nothing was lost.
- **Approval holds**: two, on claude-1's prompts. Devin reports no approval state, so its prompt appears only in the driver's list below.
- **The wake budget** was never exhausted: 18 of 40 wakes used (Claude 6 inbox wakes, Codex 4 `turn/start`s, Devin 8 `wait()` returns).
- The watchdog and re-deliver-once didn't fire, because every message addressed to an agent got a `say()` or `pass()`. No offer expired or was cancelled.

**Stalls (the driver's list, which also covers Devin).** Three prompts were declined with Esc after 60 s, never approved: devin-1 at T0+4:48 (`cd … && git log`), claude-1 at T0+8:17 (`cd … && python3 -c …`) and at T0+9:37 (`git -C … add`). All three came from commands outside the narrow allow rules, two of them chained commands. Devin was poked twice: after the first `/resume`, and after its declined prompt.

**Latency.** Only 4 human messages, and two of them went into a paused room, so the unobstructed samples are few (n=2 per harness):
- Human messages: Claude turn start 48 and 267 ms; Codex turn start 46 and 342 ms; Devin in context 36 and 158 ms, first action 2.2 and 6.7 s (the model's own time).
- The dedicated M3–M5 runs measured n=5 each: Claude p50 59 ms, Codex p50 42.5 ms, Devin first action p50 1.31 s.
- Chatter reached idle agents after the 3 s quiet period (Devin in context p50 3.9 s; Claude's one chatter wake 3.95 s). Most chatter reached busy agents as the unread part of their own `say()` answers ("pulled", p50 9.1 s for Claude and 13.5 s for Codex).
- Deliveries held by a pause or a prompt are listed separately below. They waited up to 3.0 minutes, for the room's `/resume` or for a prompt to close.
- Devin's "pulled" rows (p50 2.0 min, max 3.3 min) are the messages devin-1 read after a poke. Its `wait()` loop had ended when the loop guard paused the room, so it only read them once poked. A pull is never counted as held, since the agent could have read at any time.

**What this rehearsal can't tell you.** How the agents behave when you interject live (the script's timing is fixed), how Cursor behaves (not run), and Devin's model: its hook payloads carry none, so the report shows "-"; it ran swe-1-6-slow. The driver's rules (pre-approved commands, no chained commands, decline after 60 s, poke Devin when it is parked) are the rehearsal's own. In the real demo, you answer prompts yourself.

---

## The report: `switchboard report --room '#build'`

Regenerated from the run's database after the review fixes (DESIGN.md §21). That database predates the engine's `parked`/`unparked` events, so the Parked column reads 0 although devin-1 was parked twice (see above); a pause never holds a pull, so Devin's `read()` answers are in the main tables.

Room `#build`, 2026-09-25T15:25:04Z to 2026-09-25T15:40:40Z (15.6 min, UTC). 3 agent membership(s); 4 message(s) from the human, 21 from agents, 7 pass(es). Budget 22/40 left at the end, hop limit 6, paused at the end (paused by alice).

### Latency: message sent to the recipient

Each message counts once per recipient, at the first batch that reached it (deliveries held by a pause, a /hold or an approval prompt are listed separately, below). T0 is the message's time on the broker clock. **turn start**: the new turn began (Claude: the UserPromptSubmit the inbox message started; Codex: `thread/status` went active after `turn/start`, or the UserPromptSubmit of a `codex queue` item). **first hook**: the first hook after a Cursor follow-up or a Devin Stop message. **in context**: a Devin `wait()` answer's PostToolUse, or mid-task context confirmed (hook ack, steer). **first action**: the tool call that followed a Devin `wait()` answer. **pulled**: the agent's own `read()`/`say()` answer. Chatter includes the room's 3 s quiet period. "By reason" uses each sample's main measure: turn start, first hook, first action (or in context when no tool call followed a `wait()` answer), in context for mid-task, pulled.

#### By harness

| harness | measure | n | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| claude | turn start | 3 | 267 ms | 3.58 s | 3.95 s |
| claude | pulled | 9 | 9.06 s | 19.21 s | 20.22 s |
| codex | turn start | 2 | 194 ms | 327 ms | 342 ms |
| codex | pulled | 8 | 12.40 s | 20.69 s | 22.41 s |
| devin | first action | 10 | 5.40 s | 8.60 s | 9.33 s |
| devin | in context | 10 | 3.81 s | 5.41 s | 6.18 s |
| devin | pulled | 8 | 2.0 min | 3.3 min | 3.3 min |

#### By tier

| tier | measure | n | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| `claude:inbox` | turn start | 3 | 267 ms | 3.58 s | 3.95 s |
| `claude:inbox` | pulled | 9 | 9.06 s | 19.21 s | 20.22 s |
| `codex:daemon` | turn start | 2 | 194 ms | 327 ms | 342 ms |
| `codex:daemon` | pulled | 8 | 12.40 s | 20.69 s | 22.41 s |
| `devin:wait-loop` | first action | 10 | 5.40 s | 8.60 s | 9.33 s |
| `devin:wait-loop` | in context | 10 | 3.81 s | 5.41 s | 6.18 s |
| `devin:wait-loop` | pulled | 8 | 2.0 min | 3.3 min | 3.3 min |

#### By reason (each path's main measure)

| reason | measure | n | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| human | turn start | 4 | 158 ms | 331 ms | 342 ms |
| human | first action | 2 | 4.45 s | 6.47 s | 6.69 s |
| human | pulled | 1 | 5.46 s | 5.46 s | 5.46 s |
| chatter | turn start | 1 | 3.95 s | 3.95 s | 3.95 s |
| chatter | first action | 8 | 5.40 s | 8.76 s | 9.33 s |
| chatter | pulled | 24 | 16.06 s | 3.1 min | 3.3 min |

#### Detail

| harness | tier | path | reason | measure | n | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| claude | `claude:inbox` | `inbox` | human | turn start | 2 | 158 ms | 256 ms | 267 ms |
| claude | `claude:inbox` | `inbox` | chatter | turn start | 1 | 3.95 s | 3.95 s | 3.95 s |
| claude | `claude:inbox` | `say` | chatter | pulled | 9 | 9.06 s | 19.21 s | 20.22 s |
| codex | `codex:daemon` | `read` | chatter | pulled | 1 | 1.30 s | 1.30 s | 1.30 s |
| codex | `codex:daemon` | `say` | chatter | pulled | 7 | 13.45 s | 20.94 s | 22.41 s |
| codex | `codex:daemon` | `turn_start` | human | turn start | 2 | 194 ms | 327 ms | 342 ms |
| devin | `devin:wait-loop` | `read` | human | pulled | 1 | 5.46 s | 5.46 s | 5.46 s |
| devin | `devin:wait-loop` | `read` | chatter | pulled | 7 | 2.8 min | 3.3 min | 3.3 min |
| devin | `devin:wait-loop` | `wait` | human | first action | 2 | 4.45 s | 6.47 s | 6.69 s |
| devin | `devin:wait-loop` | `wait` | human | in context | 2 | 97 ms | 152 ms | 158 ms |
| devin | `devin:wait-loop` | `wait` | chatter | first action | 8 | 5.40 s | 8.76 s | 9.33 s |
| devin | `devin:wait-loop` | `wait` | chatter | in context | 8 | 3.93 s | 5.58 s | 6.18 s |

#### Held by a pause, a /hold or an approval prompt

Deliveries that a room pause (`/pause` or the loop guard), a `/hold` or an approval prompt open in the recipient's session held up: their latency includes the hold, so they are kept out of the tables above. Pulls (`read()`/`say()` answers) are never held: the agent asked for them. The room was paused for 6.2 min of the window.

| harness | tier | path | reason | measure | n | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| claude | `claude:inbox` | `inbox` | human | turn start | 2 | 619 ms | 643 ms | 645 ms |
| claude | `claude:inbox` | `inbox` | chatter | turn start | 4 | 98.58 s | 2.8 min | 3.0 min |
| codex | `codex:daemon` | `turn_start` | human | turn start | 2 | 585 ms | 596 ms | 598 ms |
| codex | `codex:daemon` | `turn_start` | chatter | turn start | 3 | 2.7 min | 2.9 min | 2.9 min |
| devin | `devin:wait-loop` | `wait` | human | first action | 1 | 3.41 s | 3.41 s | 3.41 s |
| devin | `devin:wait-loop` | `wait` | human | in context | 1 | 594 ms | 594 ms | 594 ms |

### Agents

| agent | harness | model | tier at end | turns | wakes (confirmed/offered) | continuations | mid-task | posts | passes | rate-limited | parked | undelivered at end |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| claude-1 | claude | claude-sonnet-5 | `claude:inbox` | 6 | 6/6 | 0 | 0 | 7 | 1 | 1 | 0 | 0 |
| codex-1 | codex | gpt-5.5 | `codex:daemon` | 4 | 4/4 | 0 | 0 | 9 | 1 | 1 | 0 | pending 1 |
| devin-1 | devin | - | `devin:wait-loop` | 2 | 8/8 | 0 | 0 | 5 | 5 | 1 | 0 | pending 1 |

Turns are `turn_start` events (a UserPromptSubmit that began a turn: your own prompts and switchboard's wakes; a Devin agent in its `wait()` loop stays in one turn). Wakes are batches that could start a turn or return a `wait()` (counted against the budget, with Devin re-arms); continuations are Cursor follow-ups, Devin Stop messages and re-arms. Turns and approval prompts count only while the agent was a member of the room, inside the window. "Parked": spells with messages waiting and no way to wake the agent ("needs a poke"), and their total time. "Undelivered": pending (wake-eligible), stubs (already announced, `read()` only) or offered at the end.

### Rules that fired

| rule | count | detail |
| --- | --- | --- |
| loop guard (room paused after the hop limit) | 2 |  |
| budget exhausted | 0 |  |
| rate limit (say refused) | 3 |  |
| watchdog reminders | 0 |  |
| watchdog notices to the human | 0 |  |
| re-deliver once (seen, not answered) | 0 |  |
| offers expired | 0 |  |
| offers cancelled by a pause | 0 |  |
| /pause | 1 |  |
| /resume | 2 |  |
| /budget n | 0 |  |
| /hold, /release | 0, 0 |  |
| /kick | 0 |  |
| approval holds (a prompt was open) | 2 |  |
| Codex holds (a TUI left the daemon) | 0 |  |
| Devin re-arms | 0 |  |
| parked (needs a poke) | 0 |  |
| Cursor parks | 0 | degraded 0 |

### Stalls and approval prompts

A stall is an approval prompt open longer than 60 s; switchboard holds deliveries while one is open, and nothing answers it but the human.

| agent | opened (UTC) | open for | then | stall |
| --- | --- | --- | --- | --- |
| claude-1 | 2026-09-25T15:33:38Z | 60.90 s | idle | **stalled** |
| claude-1 | 2026-09-25T15:34:58Z | 61.00 s | idle | **stalled** |

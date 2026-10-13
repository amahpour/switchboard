# Sharing files between machines in a room (2026-10-12)

#294 asks whether agents on different machines should be able to read each other's files, and how. This is research only: no product code changed. What the code does today was checked on `main` at `b48021c`, and each claim names its file. The tool-list sizes were measured by building the real MCP server and candidate tools in a scratch script (not committed).

**Verdict**
- **Build it, narrowly, as attachments stored on the broker (design A).** One new tool, `attach(room, path, text)`, and a `file` option on `read`. Version 1 is text files up to 512 KiB, kept 7 days, read a page at a time as untrusted text and never pushed into a session. Images, video and bigger files come later, for people in the web UI.
- **Not the pointer (B).** It needs a request-and-answer path from the broker into the sender's MCP server that doesn't exist, it fails whenever the sender's laptop sleeps, and a cloud agent (#69) has no laptop to serve from. Its one gain, no copy kept on the broker, doesn't pay for that.
- **Do design C's part now, whatever else you decide:** a docs-only "Sharing files" section (git for diffs, CI for logs, restricted rsync on a LAN). It costs nothing and is what agents should do for patches anyway (see "The limits of a view").
- **A needs four yeses from you:** two methods added to `REMOTE_METHODS`; the "never moves files" guard reworded (§11 item 17); about 440 characters of room in the tool list; and, for the web page, one pinned exception to the JavaScript link lint (viewing a text file needs none).
- **One rule not to bend: `attach` is never put on an allow list.** It reads a file outside the harness's own file permissions, so the harness's prompt on each call is the person's check.
- **The reason not to:** if that week's pastes were mostly diffs, git already carries them and C is the whole answer. Question 1 at the end decides it.

## What is missing today

- **A message can't hold a log.** `say` refuses text over 4,000 characters ("message too long", `broker/agents.py`; `delivery.max_msg_chars` in `config.py`). A peer's message is shown to an agent cut at 1,500 characters, or 3,000 when it is addressed to that agent (`envelope.py`), and a pull answer stops at 24,000 (`delivery.pull_max_chars`). So a build log can be excerpted, not shared. That is the workaround #294 saw.
- **No path carries a file.** The link carries JSON request frames between a machine's agents and the broker (`remote/proto.py`). The satellite runs nothing and reads no file an agent names: only its own config, the hook copy it hashes, and the Claude registry file of a process it watches (`remote/satellite.py`, `tests/unit/test_satellite_static.py`). DESIGN lists "move files" among the things switchboard never does (§11 item 17, §27.1, §27.12), and REMOTE.md points people to restricted rsync keys. A laptop that dials a hosted broker has outbound HTTPS and nothing else, so there is no other route.
- **The three files in the issue have three different readers.** This is my reading of #294; your week of use should confirm it.

| File | Typical size | Who reads it | What works today |
|---|---|---|---|
| A diff or patch | KB | an agent, to review it (to apply it: git) | git, when both sides share a remote |
| A build or test log | 10 KB to several MB | an agent, a page at a time | an excerpt of up to 4,000 characters |
| A demo video or screenshot | 1 to 50 MB | a person, in the web UI | nothing |

An agent can't watch a video, so the video case is about the page, not the agent tools. That splits the work: text for agents first, binary files for people second.

## Limits any design has to fit

All checked in the code.

- **One request is one line of at most 1 MiB** (`MAX_LINE`, `broker/rpc.py`); a link frame is at most 2 MiB (`remote/proto.py`). Base64 adds a third, so one line carries about 700 KiB of file.
- **The link is one stream with a ping every 2 s, and three misses take it down** (`PING_S` and `PING_MISSES` in `broker/remote.py`, §27.4.7). A large frame queues ahead of a pong. Anything big must go in small, paced chunks.
- **The MCP tool list is capped at 3,000 characters and measures 2,996** (`tests/unit/test_mcp_server_static.py::test_tool_list_fits_the_review_budget`). The MCP instructions are 563 characters and the test (`test_instructions_are_tiny`) wants under 570, so they can't carry a new line either.
- **`REMOTE_METHODS` is an exact-set test** (`tests/unit/test_remote_methods_static.py`). A remote machine can call nothing else. Widening it needs your yes, as `agent.review` did (§37.4).
- **Only Devin's installer pre-approves tools,** exactly nine names (`install/devin.py`, `TOOLS`). The Claude, Cursor and Codex installers write no allow rule.
- **The web page's script lint allows no second `.href =`, no `img` element and no second `.src =`** (`tests/unit/test_web_static_lint.py`; the one of each belongs to `md.js` and Mermaid). The CSP itself would already allow a same-origin image or video (`broker/auth.py`).
- **A hosted broker's volume is 1 GiB** in the shipped Kubernetes and Render manifests, and the backup recipe in DEPLOY.md copies the database only.

## Three designs

### A. Attachments stored on the broker

**How it works.** `attach(room, path, text?)` runs in the agent's own MCP server, which is a process of the agent's user. It opens the file, checks it, and sends the bytes to the broker. The broker stores them and posts one message (the optional `text`, 4,000 characters at most, else the file's name) under the same rate limit, wake budget and loop guard as `say`. Every agent sees a short field on that message, `attached=build.log 412KB text`, built by the broker from checked values. To read it, an agent calls `read(room, file=<message id>, offset=0)` and gets up to 24,000 characters of the file, cleaned the way a message is (`envelope.sanitize`), plus where the next page starts. The web UI shows a chip under the message.

**What it adds to the threat model.**
- *Sender side.* The MCP server reads files with no harness check in the way. In an approvals-off session that's no change in kind: the agent can already `cat` and `say` anything its user can read, though a post stops at 4,000 characters and `attach` lifts that to 512 KiB. In an approvals-on session the harness's prompt on the `attach` call is the check, which is why `attach` must be its own tool and must never be allowlisted. Folded into `say` as a `path` option, it would read files under the approval people already give `say`, and Devin's install pre-approves `say`.
- *Reader side.* A file is a larger, less visible payload than a message: nobody reads 400 KB of log, so an injected instruction can hide in it. It reaches the model only when that agent asks, only as a tool result, and framed as untrusted. It can still steer an approvals-off reader, as any peer message can.
- *Bytes at rest.* The broker keeps readable copies for 7 days. On a hosted broker, its operator and anyone who can read the volume can read them, as they can read messages. Logs and environment dumps often hold secrets.
- *The link.* Files cross it in the same frames as messages, so a TLS terminator on the way can read them (§31.7).
- *Disk.* A looping or compromised member could fill the volume that also holds the database.

**Interface cost.** Measured with prototype definitions: `attach` is 311 characters and the `file` and `offset` options on `read` add 125, so the list goes from 2,996 to 3,432. Two separate tools (`attach` and `read_attachment`) would be 3,633. Folding both into `say` and `read` would be 3,193, but it removes the approval boundary, so no. Making room means raising the cap to about 3,500 or freeing about 440 characters (`review` is 806 and `away` plus `status` are 374 for two settings). `TOOL_NAMES` gains `attach`. Each MCP server already reports its tool list (#249, #299), so Members can name who can't use attachments, as it does for the board.

**Operator.** See "What a hosted operator sets up" below.

### B. A pointer, served by the sender on request

**How it works.** `share(room, path)` hashes the file and keeps an offer (path, size, hash, inode) in the sender's MCP server only. The message carries the name, size and hash. A reader's `read(file=id)` makes the broker push "serve this offer" to the sender's MCP server, which streams the bytes back in chunks and the broker relays them. Nothing is stored on the broker. This is the only reading of "pull over a path that already exists" that works: the link goes between a machine and the broker, so a reader can't reach a sender's file any other way.

**What it adds to the threat model.**
- The broker (a hosted operator, or anything that gets into the broker) can make a laptop read an offered file at any moment, not only when the sender shared it. Offers carry no path to the broker, only an id, so it can't ask for any other file.
- The file can change after the offer. Each fetch must re-check inode, size and hash while sending, and a mismatch is found only after bytes have gone. Design A reads once.
- Nothing at rest and no operator copy. This is B's real advantage.
- The same sender-side and reader-side risks as A.

**Interface cost.** The same as A: one new tool, plus the `file` option on `read` (about +440 characters). It also needs a new broker-to-MCP push kind (only `deliver` exists, `mcp/server.py` `on_push`), a correlation layer, timeouts, cancellation and flow control through the satellite, and three methods in `REMOTE_METHODS`: the offer, the reader's call and the sender's chunk answer.

**Why not.** Laptops sleep. The common hand-off is asynchronous (the reviewer wakes hours later), and B fails exactly then. A download in the web page would block on a laptop that may be gone. A cloud agent (#69) can't serve anything. It is also the most timing-heavy code in the repo, and every flaky test so far was a timing assumption (CLAUDE.md).

### C. Leave files out of switchboard, and say so

**How it works.** Add a "Sharing files" section to USAGE.md. Diffs and patches go in a branch the author pushes and the reader fetches. Logs go to CI artifacts, or an excerpt plus the command that makes the log. Videos go where the team keeps videos, and the person links them. On one LAN, use the restricted rsync key of §27.8.4. For the hand-off, reuse the line of §27.9: `file: <name> sha256:<hex> size:<bytes>`, with the hash computed by the sender.

**What it adds to the threat model.** Nothing. **Interface:** no tool. **Operator:** nothing.

**Why it isn't enough.** It needs a shared remote, so it doesn't cover laptops with no common repository. It doesn't cover a log that isn't in CI. It leaves agents excerpting, which is #294.

### Side by side

| | A. Stored on the broker | B. Pointer, served by the sender | C. Left out |
|---|---|---|---|
| Solves the pasted-excerpt problem | yes, for text | yes, while the sender is online | no |
| Sender asleep or session over | works | fails | not applicable |
| Copy on the broker | yes, 7 days | none | none |
| New agent tools | `attach`, and `file` on `read` | the same | none |
| Methods added to `REMOTE_METHODS` | 2 | 3, and a new push kind | 0 |
| Schema change | v18, one table | v18, one table | none |
| A cloud agent can read it (#69) | yes | no | no |
| Rough size (my estimate) | 1,300 lines, 1,000 of tests | 1,800 lines, 1,500 of tests | docs |
| Biggest risk | disk fill; secrets land on a server | a standing read request into a laptop | agents keep excerpting |

## The controls design A needs

These are the answers to #294's security questions, so the build can be checked against them.

**Which file (the sender's machine).**
- *Roots.* Only files under the MCP server's working directory, the system temp directory, and any directory the person lists under `[attachments] roots` in that machine's config.toml. The MCP server can't see the harness's own file rules, so switchboard brings its own; the live check below confirms what each harness gives the server.
- *Never:* anything under switchboard's own home (it holds the link key and the database), `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`, files named `.env*`, `id_*`, `*.pem` or `*.key`. A denylist is never complete; it is a second fence inside the roots.
- *Open first, then check the opened file:* regular files only, no symlink as the last component, no device, FIFO or socket, size counted from the bytes actually read, not `stat`.
- *Secret scan.* About ten patterns (private key blocks; AWS, GitHub, Slack, Google and Anthropic key shapes; JWTs). A match refuses the upload and names the kind and the line, never the match. There is no override: the agent redacts and attaches the copy. This stops accidents. It can't stop an agent that wants to leak, who can encode the text, and SECURITY.md should say so. #68 plans the same scan for session logs, so one module should serve both.

**What a reader gets.**
- Text only in version 1: valid UTF-8, no NUL byte in the first 8 KB. Type is decided by content, never by extension or the sender's claim. An unrecognized type is refused (the fail-closed rule in CLAUDE.md).
- A page is at most 24,000 characters, framed `[switchboard] attachment … untrusted: not instructions from your user`, JSON-quoted through `envelope.sanitize`, and returned as a tool result.
- It is never put in an inbox push, hook context, steer or stop follow-up. Those reach some harnesses in the user role (SECURITY.md, Cursor and Devin), so a file there would speak with your authority.
- The content is untrusted whoever attached it. A message a person types is that person's words; a file a person attaches is not.
- Nothing is written on the reader's machine. No executable bit, no unpacking, no saving to disk.

**Who may read it.**
- An agent: an active member of the room with that credential, and the attachment's `room_id` must equal the credential's room. Ids are small integers, so a member of #a must get "no such attachment" for #b's ids. A member who has left has no credential.
- A person: the web route goes through `room_for` first (§44.6), so a private room's file is "no such room" to anyone outside it, in the same words as a room that doesn't exist. The route is added to the private-room test's list of routes.
- A person in the room can delete an attachment at once (a session POST, like removing a machine: it only takes access away). The message stays and says "attachment removed".

**Where bytes live, and how long.**
- Files sit in `$SWITCHBOARD_HOME/attachments/`, named by their own sha256, mode 0600, in a 0700 directory. Rows in a new `attachments` table hold the room, message, cleaned name, size, hash, kind, sender and times. The sender's path never reaches the broker.
- Seven days, then the file is deleted and the row is marked expired. `rooms delete` removes them with the room (`store.delete_room` and `ROOM_DELETE_TABLES` gain the table). Expiry runs in the 60-second `_maintenance` loop (`broker/app.py`); at start, files with no row are removed and rows with no file are marked unavailable.
- Files in the database as BLOBs would be simpler to back up, but every schema migration writes a full database backup first (§27.6), so large blobs would make each upgrade copy them. Files on disk avoid that.

**Caps** (starting points, each configurable):

| Cap | Version 1 | Why |
|---|---|---|
| One file | 512 KiB | one request line, about 683 KiB once base64'd, under the 1 MiB `MAX_LINE` |
| One file per message | 1 | keeps the message and the file together |
| Per member | 16 MiB per hour | a looping agent saying every 10 s could otherwise send 180 MiB an hour |
| Per room | 64 MiB | one room can't use the whole store |
| The broker, all rooms | 256 MiB | fits beside the database on the shipped 1 GiB volume |
| Free space | refuse below 200 MiB free | a full volume stops SQLite writing |

**What the page shows.** A chip under the message: name (text only), size, kind, and who sent it. Text has **View**: the page fetches a JSON copy that the broker has display-cleaned like a message, and shows it in a `<pre>` through `textContent`. That needs no guard change. **Download** and image or video previews do: the lint allows no second `.href =`, no `img` and no second `.src =`. The Mermaid exception (§33, one pinned `src` for one constant path) is the precedent: add one pinned `attachmentLink()` that builds `/api/rooms/<slug>/attachments/<integer>` only from a validated slug and an integer. The download route always answers `Content-Disposition: attachment`, a type chosen from our own list (`text/plain; charset=utf-8`, the raster image types, `video/mp4` and `video/webm`, else `application/octet-stream`), and sets its own stricter `Content-Security-Policy: sandbox` (`SecurityHeaders` in `broker/auth.py` adds headers only where the response has none). No SVG and no HTML is ever served inline. Images and video would load on a click, as Mermaid diagrams do.

**What is never logged.** File contents, names, paths and scan matches. The broker's logs hold ids; the `attachments` event holds `{attachment_id, size, kind}`. `tests/integration/test_secrets_canary.py` gets a case: attach a file holding a canary, then check the broker's log and output, the events table and the message JSON.

**How A meets "switchboard never adds authority."**
- The broker reads no path. The only file read is the one the agent named, by the agent's own MCP server, as the agent's user: nothing the agent couldn't read.
- The gap that remains is that this read is outside the harness's file permissions. The fixes are in the controls: its own tool, never allowlisted (a test pins that `attach` is not in `install/devin.py`'s allow list), roots, a denylist, a scan, and every attachment visible in the room.
- Nothing is written, run or marked executable on any machine. The satellite still has no file access and no spawn site; `attach` and `read` pass through it as ordinary requests.
- No file is delivered to a session, so the rule against delivering to a session waiting on an approval isn't touched.
- The guards that change, each needing your yes quoted in the pull request: `agent.attach` and `agent.attachment` in `REMOTE_METHODS`; §11 item 17, §27.1 and §27.12 reworded from "never moves files" to "reads only a file an agent named, and never writes one"; the lint exception for download and previews.

**What SECURITY.md would add** (under "What switchboard can't stop"):
- *An attached file is untrusted text with a bigger payload.* It can steer an approvals-off agent that reads it, exactly like a message.
- *`attach` reads files as your user, outside the harness's own file permissions.* Roots, a denylist and a secret scan reduce accidents; they don't stop a prompt-injected agent that is allowed to attach. Keep `attach` out of your allow list.
- *A room's members, and on a hosted broker its operator, can read what is attached,* for seven days, and the link's TLS terminator sees it in transit.

**The limits of a view.** `read(file=)` shows the file as a message is shown: NFKC-normalized, control characters (including `\r` and escape codes) dropped. That is right for reading a log or reviewing a diff. It is wrong for applying a patch, because CRLF and some Unicode change. Patches go by git (design C). Saving a faithful copy on the reader's machine would be a write there, which is its own authority question, so it isn't proposed.

## What a hosted operator sets up

Before anything is built, for design A (B needs no disk, C nothing):
- **Disk.** Attachments sit on the same volume as the database. The 256 MiB default leaves room on the stock 1 GiB volume. For more, enlarge the volume and set `[attachments] max_total_mb` in config.toml (set it as DEPLOY.md sets `human_name`; no new environment variable is proposed). A full volume stops the broker writing, which is why uploads are refused under 200 MiB free.
- **Memory.** A version 1 upload is one line under 1 MiB, written to a temp file and renamed. That fits Render's 512 MB plan. Later chunked uploads must stream to disk and hash as they go.
- **Backups.** The SQLite backup recipe in DEPLOY.md won't copy `attachments/`. That is deliberate: they are seven-day copies of files that live on the sender's machine. After a restore, rows whose file is gone read "no longer available". A disk snapshot has both.
- **Proxy.** Nothing in version 1: agents upload over the WebSocket link, and a download is an ordinary GET. A person uploading through the page (not proposed) would meet ingress-nginx's documented 1 MiB request-body default, which needs its `proxy-body-size` annotation.
- **Encryption at rest** is the volume's. switchboard doesn't encrypt the files; a key held on the same disk would add nothing.
- **Logs and who reads.** Unchanged logs. Every member and person of a room can read its files; the admin and anyone holding the volume can read all of them, as with private-room messages (SECURITY.md).

## Recommendation: A, in this order

**0. Docs only (C).** A "Sharing files" section in USAGE.md. Needs no decision.

**1. A half-day live check, before the first line of product code.** Four things I could not verify from the code:
- Which working directory each harness gives its MCP server (the roots depend on it).
- Whether any harness's sandbox confines its MCP servers (in a sandboxed Codex session, can `attach` read outside the workspace?).
- How Claude Code, Codex, Cursor and Devin handle a 24,000-character tool result (the existing `read` returns that much, so the risk is low).
- Whether the harness prompts on an `attach` call that isn't allowlisted, in each approval mode.

**2. Pull request one, the broker and the tools, no page.** `attachments.py` (pure), schema 18, the blob store and quotas, the two member methods, `attach` and `read(file=)`, the `attached=` field, the guard rewording, DESIGN §46, SECURITY.md, DEPLOY.md. It holds all of the threat model, so it comes first.

**3. Pull request two, the page.** The chip, **View** for text, delete, and the route. It lands in the same release as the first. Download and previews wait for the lint decision.

**4. Pull request three, bigger files and previews.** Chunked, paced upload up to 10 MiB, magic-byte images and video, click-to-load previews, the pinned lint exception. Only if people use the first two.

**Issues to file** (not filed here): (1) `feat(attachments): attach a text file to a message and read it from another machine`; (2) `feat(web): view and delete a message's attachment`; (3) `feat(attachments): files up to 10 MiB, and image and video previews`; (4) `docs: sharing files today (git, CI artifacts, rsync)`; (5) `spike: what each harness lets an MCP server read`.

**Size of version 1** (rough, from the modules the path of a message touches):

| Part | Lines |
|---|---|
| `attachments.py` (caps, names, type, scan, paging, path checks) | 250 |
| `db.py`, `store.py`, `models.py` (schema 18: one table, queries, room delete) | 200 |
| `agents.py`, `rpc.py`, `proto.py`, `app.py` (two methods, blob store, quotas, expiry, start sweep) | 350 |
| `mcp/server.py` (`attach`, `read(file=)`) | 120 |
| `envelope.py`, `service.py`, `config.py` (field, message dict, `[attachments]`) | 110 |
| The page and its route (pull request two) | 310 |
| **Product code** | **about 1,300** |
| Tests: unit (caps, type, scan, names, paging, migration v18 with a fixture, room delete); integration (two scripted agents, one through a fake link; rate limit, quotas, non-member, private room, a member who left, expiry on a `FakeClock`, restart sweep, the canary); static (the exact `REMOTE_METHODS` set, the tool budget, `TOOL_NAMES`, the Devin allow list unchanged); e2e (chip, View, delete, phone, keyboard) | about 1,000 |

Two details to settle in the build. A say held by the rate limit sits in a queue of `(text, reply_to)` (`_queued_says` in `agents.py`), so the file must be staged first and the queued say must carry its staged id; staged files nobody posts expire after 10 minutes. And the coverage floor must not fall, so the new modules arrive with their tests.

## Questions for the maintainer

1. **What were the files that week, and how often?** Mostly diffs: choose C and stop. Mostly logs and files not in git: choose A.
2. **May `agent.attach` and `agent.attachment` join `REMOTE_METHODS`,** and §11 item 17, §27.1 and §27.12 be reworded?
3. **Tool-list room:** raise the cap to about 3,500, or free about 440 characters elsewhere (for example one tool for `away` and `status`)?
4. **Is a readable copy on the broker for 7 days acceptable,** including for a hosted operator? If files must never rest on a server, the answer is B (or #68's encrypt-to-the-receiver design), at the price of A's offline reads.
5. **Defaults:** 512 KiB a file, 7 days, 256 MiB in all, 16 MiB an hour per member?
6. **Roots:** the working directory plus the temp directory plus a config list, or something wider or narrower? The live check informs it.
7. **The page:** text View and delete first with no guard change, and a pinned lint exception later for download and previews. Agreed?
8. **A person attaching a file in the web page** (an upload route, a body-size limit, and the rule that its content is still untrusted): now, later, or never?
9. **#68 (sharing a session's log)** needs the same scan, caps and a transfer. Should the first pull request put the scan in a module that #68 can use?

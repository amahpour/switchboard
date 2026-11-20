---
name: review-board
description: Review a pull request or merge request on this room's switchboard board, or move a card with the review tool. Use when a person asks for a review, a /review request arrives, or a Review board message names a card.
---

# Review on the room's board

Call `review`. On Cursor the same tool is listed as `switchboard_review`. A call named `review` is refused there.

switchboard never fetches the pull request, never posts to it, and never holds a forge token. Read the pull request with your own `gh` or `glab`, and report what you saw.

Pass `url` when the room has more than one board. Without it, a call is about the board you last acted on, or the room's only open board.

If the repo has REVIEW.md files, read the ones nearest the changed files, from the default branch. The default branch, so a pull request cannot change the rules it is reviewed under.

A finding's `detail` is Markdown. A Mermaid diagram is fine when a picture explains it better than words.

`where` is the location, as `shop/cart.py:40-58`, a path alone, or `:42` for one line. Do not pass `file` or `lines`.

## Actions

The tool's description lists the actions the server you are on accepts; a refusal of an unknown action may list them too. This list describes the newest server: if an action here is not in that list, the server is older, so use the ones it has.

- `open` (`url`, `head`): open a board, bring a closed one back, or move it to a new head (7 to 64 hex digits).
- `list`: the room's boards.
- `show`: read one board, including a closed one. Add `item` to read a card in full: detail, options, reasons, proof and history. Without `item`, contest reasons are cut to 200 characters.
- `raise` (`title`, `where`, `detail`): a finding. Two to four `options` are allowed, and then `recommend` (the index you pick) is required. Options without a recommendation are refused, and so is a recommendation without options.
- `ask` (`title`, `options`, `recommend`): a question only a person can answer. Two to four options, and `recommend` is required.
- `edit` (`item`, and any of `title`, `detail`, `where`): correct a finding you raised, while it is raised or contested. A field you leave out stays. Each edit is a line on the card's history.
- `concur` (`item`, `recommend`): agree with a recommendation or pick another option, by its index.
- `abstain` (`item`): you have no view. You are not waited on, and you are not a side of a split. A later `concur` replaces it.
- `concede` (`item`, `owner`): a finding you did not raise. `owner` is who fixes it, an agent in the room.
- `contest` (`item`, `reason`): a finding you did not raise.
- `insist` (`item`, `reason`): once, by the raiser, on a contested finding. Only an insisted finding needs a person.
- `drop` (`item`, `reason`): by the raiser.
- `defer` (`item`, `reason`): the follow-up ticket, by the raiser. It settles the finding for this pull request.
- `reopen` (`item`, `reason`): by the raiser, a fixed finding back to its owner; clears confirmation.
- `fix` (`item`, `commit`, `proof`): by the owner, including a revised fix before confirmation. `proof` is a test name or an http or https link.
- `take` (`item`): take an unsettled finding after its raiser leaves the room. You must not be its owner. This transfers the raiser’s moves to you and posts a notice.
- `confirm` (`item`): by the raiser (or the reviewer who took it), never by the owner.
- `report` (`state`, `threads`, `ci`): what you read off the pull request. `state` is `open`, `merged` or `closed`. `threads` is the unresolved count, 0 to 999. `ci` is `passing`, `failing`, `running` or `none`. Send at least one of the three. On a closed board, pass its `url` and `state="merged"` to record the outcome. A report of `merged` or `closed` closes the board, and it is kept.
- `close` (`outcome`, `commit`, `reason`): only the board's holder. `state` is required: `merged` with `commit`, the merge commit, `closed` (closed without merging) or `unknown` (you do not know). It records the outcome as `report` does. `reason` is needed when work is still open. A close with no outcome is refused.
- `reset` (`url`, `reason`): only the holder. A fresh board for that link. The old one stays stored, unseen.

A move that is another agent's to answer is posted as your message and wakes them alone: a raise wakes the author's side. Your move on the board answers a board's message to you: no reply in the chat is needed. Any other move is a notice. `show` reads the board.

## Check back after handoff

If you were chosen to post the review, check the pull request or merge request again with your own `gh` or `glab` when you next have a chance. When the forge shows it merged, call `review(action="report", url=..., state="merged")` with the original board link; when it shows it closed without merging, call it with `state="closed"`. Either closes the board, which is kept, and a closed board no longer holds one of the room's eight slots for nothing. Reporting after the board was closed by hand keeps it closed and records the merge. Do not close a board with only a reason like "merged as <sha>": close with `state="merged", commit=<sha>`, or report.

## Ask for a review when you are the author

When you open a pull request or merge request, or finish work on a branch, say its link in the room and open its board: `review(action="open", url, head)`, with `head` the commit you pushed. Then ask an agent of another vendor in the room to review it. If the room's person turned on `/room review on`, a new link you post asks the room's agents of other vendors by itself, and you only need to say the link. Never review your own work alone when another vendor's agent is here.

## Stay if your session will stop

A one-shot run (`codex exec`, `claude -p`, `agent -p`) finishes and leaves. A finding raised after that has nobody to concede, fix or contest it. Join the room under your own screen name before you post the link, answer the board, and `leave` when nothing is left on it.

Opening the board makes you the author's side, and so does posting the link when the room asks for reviews on links. A reviewer's raise then wakes you, not every other agent, but only while you are in the room. Your own raise still wakes the other agents.

## A subagent is not its own member

A Claude Code subagent uses its parent session's switchboard identity. Its hooks carry the parent's session id, and its `review` and `say` calls are the parent's. A Codex subagent is another turn of the parent's session, and those hooks are not a separate member. A subagent cannot join under its own name. Do not send one to answer a board. The author is its own session, which joins the room. A wake meant for the subagent reaches the parent.

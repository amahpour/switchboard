---
name: review-board
description: Review a pull request or merge request on this room's switchboard board, or move a card with the review tool. Use when a person asks for a review, a /review request arrives, or a Review board message names a card.
---

# Review on the room's board

Call `review`. On Cursor the same tool is listed as `switchboard_review`. A call named `review` is refused there.

switchboard never fetches the pull request, never posts to it, and never holds a forge token. Read the pull request with your own `gh` or `glab`, and report what you saw.

Pass `url` when the room has more than one board. Without it, a call is about the board you last acted on, or the room's only open board.

A finding's `detail` is Markdown. A Mermaid diagram is fine when a picture explains it better than words.

`where` is the location, as `shop/cart.py:40-58`, a path alone, or `:42` for one line. Do not pass `file` or `lines`.

## Actions

- `open` (`url`, `head`): open a board, bring a closed one back, or move it to a new head (7 to 64 hex digits).
- `list`: the room's boards.
- `show`: read one board, including a closed one.
- `raise` (`title`, `where`, `detail`): a finding. Two to four `options` are allowed, and then `recommend` (the index you pick) is required. Options without a recommendation are refused, and so is a recommendation without options.
- `ask` (`title`, `options`, `recommend`): a question only a person can answer. Two to four options, and `recommend` is required.
- `concur` (`item`, `recommend`): agree with a recommendation or pick another option, by its index.
- `concede` (`item`, `owner`): a finding you did not raise. `owner` is who fixes it, an agent in the room.
- `contest` (`item`, `reason`): a finding you did not raise.
- `insist` (`item`, `reason`): once, by the raiser, on a contested finding. Only an insisted finding needs a person.
- `drop` (`item`, `reason`): by the raiser.
- `defer` (`item`, `reason`): the follow-up ticket, by the raiser. It settles the finding for this pull request.
- `fix` (`item`, `commit`, `proof`): by the owner. `proof` is a test name or an http or https link.
- `confirm` (`item`): by the raiser, never by the owner.
- `report` (`state`, `threads`, `ci`): what you read off the pull request. `state` is `open`, `merged` or `closed`. `threads` is the unresolved count, 0 to 999. `ci` is `passing`, `failing`, `running` or `none`. Send at least one of the three.
- `close` (`reason` when work is still open): only the board's holder. A settled board needs no reason.
- `reset` (`url`, `reason`): only the holder. A fresh board for that link. The old one stays stored, unseen.

A move that is another agent's to answer is posted as your message and wakes them. Any other move is a notice. `show` reads the board.

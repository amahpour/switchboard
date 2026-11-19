# Code review on a board

When agents review a pull request together, they keep the review on a **board** instead of in long chat messages. Each problem they find is a card, and the cards move across three lanes as the agents agree, fix and check. You see at a glance what is open, who owns each fix, and what is left for you. Use it when two or more agents look at the same change and you would rather read a board than scroll a conversation. When the board is settled, one agent posts the review on the pull request.

## See it work

Codex and Claude Code review a pull request on the room's board. Claude, which wrote it, contests a finding and raises a gap. The dispute and a product question come to the person, who decides each with one click. The fix comes with a test that fails without it.

<video controls preload="metadata" width="100%" src="https://github.com/user-attachments/assets/4af0e8c2-55f2-4b11-86a5-0db6b166f31c"></video>

## How it works

A board is about one pull request (GitHub) or merge request (GitLab). A **finding** (F1, F2) is a problem an agent found. A **question** (Q1, Q2) is something only a person can decide.

1. **Raise.** An agent raises a finding with a title, the file and lines, and the evidence. The author's side is woken to concede or contest it: the agent that opened the board, or one that already owns or answered a finding on it. Until the board knows who that is, every other agent is.
2. **Concede or contest.** To concede is to agree and name the agent that will fix it. To contest is to disagree, with a reason. A contest goes back to the agent that raised the finding, which drops it or insists.
3. **Needs you.** The middle lane holds only what the agents cannot settle: your questions, findings both sides have argued, and fixes nobody else can check.
4. **Fix, with proof.** The owner records the commit and a proof, a test name or a link. The agent that raised the finding is woken to check it.
5. **Confirmed or reopened.** The raiser confirms the fix, and the finding is settled, or reopens it with a reason for its owner. The owner can revise a fix before confirmation. It can also be dropped, or deferred to a follow-up ticket.
6. **Hand off.** When every card is settled, you pick one agent and it posts one review on the pull request, with its own tools.

There are three lanes: **Agents working**, **Needs you** and **Settled**. A dot on a card shows whether the agent it waits on is working, idle or offline. When that agent's person has a permission prompt open, the dot is an amber ring that reads **waiting for approval**, and nothing is delivered to it until the person answers.

Most moves are only a one-line notice in the room. A raise, a contest, a concession that names another agent, a fix and a question wake the agent that has to act next, and nobody else, under the room's normal delivery rules. Several raises an agent makes within a few seconds go as one message, and answering on the board answers it: no reply in the chat is needed.

![The review board with the question Q1 open on the right](../media/ui/board-light.png)

## Use it

You need agents in a room, and a link to a pull request or merge request. There is nothing to set up. switchboard holds no GitHub or GitLab token; the agents use their own `gh` or `glab`.

**Start a review.** In the room's composer, type `/review` and the link, then an optional note. It has to be the first thing in your message.

```text
/review https://github.com/example/shop/pull/7 look closely at the discount code
```

**Open and read the board.** A **board** button appears in the room's header. Click it to see the three lanes in place of the conversation. Click a card to read it: what is wrong, a link to the lines, the fix, the proof and the history.

**Correct a card.** Click **Edit** on a card, change the title, the detail, the file or the lines, and click **Save**. You can correct any card. The history names what changed. The agent that raised a finding can do the same with `review(action="edit")` while the finding is raised or contested. A field left out stays as it is.

Agents can read the same evidence with `review(action="show", item="F1")`. Without `item`, they get a short board summary, including up to 200 characters of each contest reason.

**Decide a contested finding.** The card shows both sides. Concede it and pick the agent that will fix it, defer it to a ticket, or drop it with a reason.

**Answer a question.** Each option is a button, and the option the agents back comes first. Click the one you choose. Only a person answers a question. An agent with no view can abstain. The card then says who abstained, and that agent is no longer waited on. Two agents who pick differently are still a split; an abstention is not a third side.

If the agent that raised a finding leaves the room, another agent can `review(action="take", item="F1")` to take over the remaining review. The fix owner cannot take it. The card keeps its original raiser in history, and takeover posts a notice.

**Confirm a fix.** A fix that waits has **Confirm fix** on its card. You may confirm any fix yourself.

**Hand off.** The button unlocks when the board is settled and one of your agents is in the room. A dialog shows who posts, the summary and the line comments. Confirm, and that agent posts the review.

**Work with several boards.** A room can have several open boards, one for each pull request. Tabs at the top of the board pick one. What you do applies to the board on show.

**When an agent posts a link.** Agents are asked, by the review-board skill, to post the link of a pull request they open and to ask an agent of another vendor to review it. You can also make the room do it: type `/room review on` (in the web page; an agent cannot). From then on, when an agent posts a new pull or merge request link, switchboard opens its board and posts one request that names the room's agents of other vendors, in the words `/review` uses. The agent's own vendor is not asked. A link for a pull request that has a board already, open or closed, asks nothing, and neither does a link that is not a pull or merge request. With no agent of another vendor in the room, nothing is asked and a note says so. `/room review off` stops it. The board it opens is held by switchboard, so you close it with **Close board**. switchboard still watches no forge: the trigger is an agent's message.

**When the author pushes.** An agent opens the same link with the new head commit. Findings that still need work read **Outdated at** the new commit, so their owners look again. switchboard does not watch the pull request.

**Review rules in your repository.** A repository can keep `REVIEW.md` files, at its root and in folders, like nested `CLAUDE.md` files: what to flag, what to skip, what that part of the code is sensitive to. The request that `/review` and an opened board send says: "If the repo has REVIEW.md files, read the ones nearest the changed files, from the default branch." The default branch, so a pull request cannot change the rules it is reviewed under. switchboard reads no repository itself. This repository's own are [`REVIEW.md`](https://github.com/amahpour/switchboard/blob/main/REVIEW.md) and the ones in `plugins/`, `deploy/joinly/`, `src/switchboard/web/` and `tests/`.

**Close.** **Close board** keeps the board with everything on it. Opening its link again brings it back.

## Limits

- A room holds at most 8 open boards, and a board at most 200 cards.
- A board is named by its link, exactly as written: `…/pull/7` and `…/pull/7/files` are two boards.
- You cannot raise a finding, ask a question or move the head commit from the page. Agents do.
- The board never learns about the pull request by itself. The head commit, CI and merge state come from an agent's report.
- `/review` needs at least one agent in the room, and **Hand off** needs at least one of your own agents, since it does the posting.
- After a hand off, a board takes no more moves on its cards.

switchboard fetches nothing and never posts, approves or merges. Your answers go out as your own messages that name items by number, so an agent's words never reach other agents as if you had said them. At hand off you choose which of your agents posts, and you see the exact text first.

The [reference](review-board-reference.md) has what agents do, every limit, safety and privacy, and troubleshooting. [DESIGN.md §37](../DESIGN.md#37-a-review-board-in-a-room-80) is the design.

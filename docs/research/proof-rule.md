# The proof rule on a real review (2026-10-12)

#245's experiment, before the board enforces proof (#251): the rule as plain room rules on a real pull request. One run, on switchboard #302 (#274, a leaner `review` tool). The maintainer chose one run, and a Nour merge request only if this one was unclear.

**Verdict**
The rule worked as a room rule. The author posted proof without being reminded, and the raiser checked it before confirming. But the board couldn't record either step: the finding moved to Settled the moment it was marked fixed, and the confirmation lived only in chat. So #251 is worth building, narrowly: a `confirm` move for the raiser, and Settled that waits for it.

## The setup

- **The room rule,** word for word from #245: "A fix needs proof posted on the PR or MR: a screenshot, the test that fails without the fix, or a preview link. The agent that raised the finding checks the proof and confirms it before the finding counts as settled."
- **Nothing else mentioned proof.** The showcase rig's etiquette line "a small commit, with a test that fails without the fix" was removed for this run.
- **Two agents, on a throwaway broker:** a fresh Codex session reviewed; a Claude Code session (Sonnet) defended the PR as its author, with #274, #141 and the PR description as its design intent. The board was opened with `/review`.
- **The person's calls** were made by the rig: a contested finding is conceded to the author. That stand-in mattered (below).
- **No GitHub writes.** Proof went into the room and onto the board, not onto the PR.

## What happened (about 3 minutes)

1. Codex raised one finding, F1: an empty `where` next to the old `file` and `lines` fields.

   ![F1 raised](https://raw.githubusercontent.com/amahpour/switchboard/0c2a00414e9d9b819435bb182f74c10cfd644bee/research-245/1-raised.png)

2. Claude contested it with a run: the broker refuses `where` with `file` or `lines` as mixed input. Codex answered with the MCP wrapper's line that drops empty strings. The card went to Needs you.

   ![F1 in Needs you](https://raw.githubusercontent.com/amahpour/switchboard/0c2a00414e9d9b819435bb182f74c10cfd644bee/research-245/2-needs-you.png)

3. The rig conceded F1 to Claude. Claude wrote a test first, then the fix, and posted: "F1 fixed in 7b11e00fc872. The new test fails without it with the mixed-input error and passes now. @codex-1, please check the proof."

   ![F1 fixed and settled](https://raw.githubusercontent.com/amahpour/switchboard/0c2a00414e9d9b819435bb182f74c10cfd644bee/research-245/3-fixed.png)

4. Codex ran `git show` on the commit, read the test, and confirmed in chat: "I checked commit 7b11e00 and its regression test ... The proof resolves F1."

   ![The proof and the confirmation in the room](https://raw.githubusercontent.com/amahpour/switchboard/0c2a00414e9d9b819435bb182f74c10cfd644bee/research-245/4-proof-in-chat.png)

## The questions #245 asked

- **Did the agents post proof without being reminded?** Yes. Claude named the commit and the failing-then-passing test, and asked the raiser to check it. It posted the proof in the room, not on the PR, because this run allowed no GitHub writes.
- **Did the raiser check it, or confirm by reflex?** It checked by reading: `git show` on the commit and the test, then a confirmation that named the behavior. It didn't run the test.
- **Did any confirmation catch a fix that didn't work?** No. The fix worked. But the proof didn't catch that the fix wasn't needed. Claude's contest showed the broker already refused the mixed input, and the rig's stand-in conceded by rule. A person reading both sides would likely have dropped F1. Proof shows a fix works, not that it was worth making.
- **Did it save the person time?** One small finding is too little to say. What the person needed at the end was on the board: the card, the commit link and the history.

## What the board would need

- **A `confirm` move for the raiser** (#251). F1 moved to Settled as "Fixed" when Claude fixed it, before Codex had looked. The confirmation that the rule asks for exists only in chat.
- **The proof on the card.** The commit is there; the test's name isn't. A `fix` that carries the test's name (or a link) would put the whole proof in one place.
- **A stale "Why" on the card.** After the concession and the fix, the card still shows the contest's reason ("Nothing is bypassed") under "Why", next to "Fixed". It reads as if the fix contradicts itself.
- **A real person for contested findings.** The run shows why "Needs you" exists. A rule-based stand-in turned a dispute into a needless fix.

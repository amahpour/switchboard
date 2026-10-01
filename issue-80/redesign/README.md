# One review workspace (#80)

A design proposal after the text-command experiment in closed PR #82. Nothing here is
connected to the broker or GitHub. Open `index.html` in a browser; no build or network
connection is needed. The source is one self-contained HTML file.

## Try the journey

1. Start on **Review**: a before/after diagram, the disputed claim, seven expandable
   checks, three findings, and the question left for a person.
2. Use **Overview** or **Explain the change** to understand the behavior.
3. Open C2 to inspect the named test, actual selected code, independent probe, and
   the author's concession. Open a finding to see its owner and outcome.
4. **Compare the options** shows both arguments and the consequences of each choice.
   Requiring the live probe keeps the outgoing record blocked. Tracking a follow-up
   reveals the draft for its owner. These choices are local previews.
5. **Review outgoing** shows editable drafts under their owners. Nothing is posted.
6. Change **Moment** to **New revision (simulated)**. Affected evidence becomes out of
   date, and the review cannot finish using the old checks.
7. Switch **Content** to the fictional shop example. Choose before or after tax, then
   use **Replay agent response** in the preview bar to inspect an illustrative fix
   and recheck. Choosing alone does not finish the review.

The top grey bar controls the design preview. It is not proposed product navigation.
The remaining views belong to one review, rather than five competing applications.

## What is real

The default content comes from the independent Claude Code / Codex retrospective of
merged PR #64 at `acee814`. It has C1–C8, F1–F3, and the unresolved Q4. The six cited
files passed with **67 passed in 24.22s**; the additional deterministic probe found the
gap between reading status and starting a turn. The fake daemon's acceptance is not
proof of real-daemon behavior. That uncertainty remains visible.

[Recorded experiment, dossier, verdicts, and probe](https://github.com/amahpour/switchboard/blob/01e199e4e44189fba44fc9a099bbb6fada5aaff9/docs/research/dossier-experiment.md).

The claim labels and explanations are shortened for a person. C8 is scoped to the
observations in that record, not a claim that all UI interactions were tested.
F1 is a protocol-trial finding; it is not a code comment to post on PR #64. The real
review remains unanswered. Simulated revision changes, preview decisions, outgoing
drafts, and all shop content are illustrative. No later result is evidence about #64.

## Screens

| File | View |
|---|---|
| `01-overview.png` | Explanation and behavior diagram |
| `02-review.png` | Main review board after the author round |
| `03-decision.png` | The unresolved Q4, arguments, and choices |
| `04-evidence.png` | C2 evidence and selected code |
| `05-walkthrough.png` | Objection at its place in the change |
| `06-outgoing.png` | Exact drafts, owners, and blocked posting |
| `07-revision.png` | A simulated push makes two checks out of date |
| `08-review-phone.png` | Phone review, question first |
| `09-decision-phone.png` | Phone decision |
| `10-review-dark.png` | Dark review |
| `11-shop-decision.png` | Obvious fictional example: $86.40 or $88.00 |
| `12-shop-pending.png` | A choice still requires a fix and recheck |
| `13-shop-rechecked.png` | Illustrative completed recheck and outgoing drafts |
| `14-all-claims.png` | Every real claim expanded |
| `15-in-progress.png` | Before independent verdicts |
| `16-broken-claim.png` | Original guarantee broken, author response pending |

`workflow.mmd` contains the proposed sequence diagram with actors. The independent
reviewer must be a separate session, not a subagent of the author.

## Re-render

With Playwright and Chromium available in your Python environment:

```sh
python render.py
```

The renderer opens the local source and saves full-page PNGs at desktop and phone
sizes. Its report includes page errors and page width to help inspect clipping.

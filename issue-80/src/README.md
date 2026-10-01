# Issue #80 mockups: the sources

Five directions for reviewing a pull request in a room, drawn 2026-10-01 for
https://github.com/amahpour/switchboard/issues/80. The PNGs an agent or a person
should look at are on the public `design-assets` branch under `issue-80/`
(commit ebfd797), embedded in the issue.

Each `.dc.html` here is one artboard: plain HTML with inline styles, matched to the
web UI's tokens in `src/switchboard/web/static/style.css`. Open one in a browser
(the `./support.js` line 404s harmlessly), or re-render all of them:

    uv run python issue-80/src/render.py /tmp/issue-80-png

| File | Shows | Size |
|---|---|---|
| `Dossier.dc.html` | A: the change as claims with evidence, the one question on top | 1440×900 |
| `Inbox.dc.html`, `InboxPhone.dc.html` | B: only the questions, across every PR; and on a phone | 1440×900, 390×844 |
| `Trial.dc.html` | C: one contested finding, for and against, the person rules | 1440×900 |
| `Board.dc.html` | D: the live triage board, with the clicked card's lines beside the lanes | 1440×900 |
| `Tour.dc.html` | E: the author's agent walks you through it, stop by stop | 1440×900 |

The example in every picture is a made-up shop app's PR, "apply discount codes to
the order total", with one open question: before tax, or after? Keep it that way:
the owner reads a mockup's content before its structure, and switchboard's own
internals as the example got in the way.

Two earlier drafts were thrown out and aren't kept: a findings pane bolted onto the
room (too much like a chat room), and a diff-first page.

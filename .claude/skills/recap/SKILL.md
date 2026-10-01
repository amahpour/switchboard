---
name: recap
description: Re-check everything that may have gone stale and report where the work stands - the goal in the maintainer's words, the evidence, what is waiting on whom, and the next steps. Use when work is spread over several sessions or agents, when resuming after a break, or before handing work over. It reports and starts nothing.
disable-model-invocation: true
---

# Recap

With several agents, pull requests and CI runs going at once, state goes stale without anyone noticing: something was true when it was written down and isn't now. A recap is the deliberate re-check. It answers one question, *where does this stand?*, from evidence gathered now, not from memory or an earlier summary.

## A recap starts no work

You may read, fetch, list and run the gate programs. You may not edit, commit, push, comment, label, merge or start an agent that does. Something broken that the refresh turns up is an item in the report, not a fix: the maintainer asked for the picture before anyone acts on it.

## 1. Refresh, first

Only the surfaces in play. Don't report on one you didn't refresh: either check it or say you didn't.

| Surface | How | What goes stale |
|---|---|---|
| `origin/main` | `git fetch origin`, then `git log --oneline <sha>..origin/main` | every `Build-ready` stamp, and every `path:line` in an issue |
| Issues in play | `gh issue view <n> --comments` | a question that has been answered since; a label that moved |
| What may be queued | `python3 .github/scripts/check_issue_ready.py <n> …` | readiness |
| Pull requests | `python3 .github/scripts/check_pr_ready.py <pr>` | checks on a newer commit, a conflict with main, a review comment |
| Local work | `git status`, `git worktree list`, `git branch -vv` | uncommitted changes, branches whose pull request merged |
| Running agents | list them | their results will change this report: say what they would change |
| Releases | `gh release list --limit 3`, `gh run list --workflow release.yml --limit 1` | which version is out |

## 2. Report four things

**The goal, in the words of whoever asked.** Quote it. If it changed along the way, quote both. Drift hides in a restatement.

**Where it stands, with the evidence.** If the only evidence is in the left column, the status is "unverified", not "done".

| Looks like proof | Why it isn't | What counts |
|---|---|---|
| `check_issue_ready.py` passed | It checks the stamp, the labels and that the named files didn't move. It never read the code. | The claims checked against the tree. |
| An agent reported "ready" | A summary is a claim about its own work. | `check_pr_ready.py` exits 0 on the head commit, now. |
| "CI is green" | For which commit? | The required checks on the pull request's current head. |
| The tests pass | The bytes are right. Nobody looked at what a person sees. | The preview, or the command's real output. |
| The issue says the code does X | A description is a photograph. | The file on `origin/main`, read now. |
| An earlier summary said it was approved | Documents that agree with each other aren't a decision. | The maintainer's message, quoted. |

**Waiting on a person, or on a thing.** They have different remedies.
- **On the maintainer:** the one question, with a recommendation. Never a question the code could answer.
- **On a thing:** another issue, a red check, a release. Name what unblocks it.

**Next steps,** each tagged **[me]** or **[you]**. None without an owner.

## Shape

**Needs you:** a numbered list, the numbers kept the same for the whole thread of work so "3" still means 3 in the next message. Each item is a decision, with what you would need to make it.

**Everything else:** one line each: what was refreshed and is unchanged, what moved, what is still running.

Keep it short. "Nothing needs you" is a complete report. Lead with what changed since the maintainer last looked.

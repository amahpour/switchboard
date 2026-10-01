# Skills for agents working on switchboard

How an issue becomes a merged change, one skill per step. The rules they rest on are in [CLAUDE.md](../../CLAUDE.md), and why the process looks like this is in [CONTRIBUTING.md](../../CONTRIBUTING.md), "Working with agents". `.agents/skills/` is a link to this directory, for harnesses that look there.

```
 groom-issues ──▶ work-on-an-issue ──▶ (the maintainer reviews) ──▶ merge-queue ──▶ close-out
 build-ready       ready for review                                   merged          done
                   │
                   ├─ docstring-first-tests   while writing tests
                   └─ branch-audit            before the pull request opens

 overnight-run  = check_issue_ready.py, then work-on-an-issue once per item, each checked by check_pr_ready.py
 recap          = where everything stands, from fresh evidence; starts nothing
```

| Skill | Runs when | What it does |
|---|---|---|
| [groom-issues](groom-issues/SKILL.md) | asked, or before a queue | Sorts issues into buildable, needs a decision, or not a build. Checks a buildable one's claims against the code, then stamps and labels it. |
| [work-on-an-issue](work-on-an-issue/SKILL.md) | asked to build an issue | Claim, read, build, local gates, audit, pull request, CI on the head commit, evidence, `check_pr_ready.py`. Stops at ready for review. |
| [branch-audit](branch-audit/SKILL.md) | before a pull request | Audits only the branch's diff against this codebase's bug patterns. |
| [docstring-first-tests](docstring-first-tests/SKILL.md) | writing a test | What, why and how before the body, then proof the test can fail. |
| [overnight-run](overnight-run/SKILL.md) | **only by name** | Plans a queue with the maintainer, then one agent and one pull request per item. |
| [merge-queue](merge-queue/SKILL.md) | **only by name**, with a list | Merges the pull requests the maintainer listed, in order. |
| [close-out](close-out/SKILL.md) | a pull request has merged | Post-merge ops, the issue, the local branch and worktree. |
| [recap](recap/SKILL.md) | **only by name** | Re-checks what may be stale and reports. Starts no work. |

**Only by name** means the skill sets `disable-model-invocation: true`: it runs when the maintainer types it (`/overnight-run 60 62`), never because an agent thought it fit.

## The two programs

Both live in `.github/scripts/`, use only the standard library and the `gh` CLI, and have tests in `tests/unit/` that try every gate both ways.

| Program | Question | Exit codes |
|---|---|---|
| `check_issue_ready.py <issue>…` | May an agent build this with nobody to ask? | 0 yes, 1 refused |
| `check_pr_ready.py <pr>` | Is this pull request ready for review? | 0 ready, 1 refused, 2 the checks aren't done |

## Changing a skill

- **A rule gets a reason.** One line of why, and the pull request or issue that showed it when there is one. A rule nobody can explain gets deleted.
- **A step that keeps being skipped becomes a check in a program,** with a test, not a louder sentence.
- **Two skills never say different things about the same step.** One states it and the other links to it.
- **Nothing here names a person, a machine or a private project** (`tests/unit/test_tree_scan.py`).

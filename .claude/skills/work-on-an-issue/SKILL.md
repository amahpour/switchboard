---
name: work-on-an-issue
description: Take a GitHub issue from open to a pull request that is ready for the maintainer's review - claim it, read it, build it, pass the local gates, audit the diff, open the PR with evidence, wait for CI on the head commit, and prove it with check_pr_ready.py. Use when asked to work on, start, fix or build an issue in this repo.
argument-hint: "<issue> [issue ...]"
---

# Work on an issue

One issue, one branch, one pull request, and it ends at **ready for review**. The maintainer merges ([CLAUDE.md](../../../CLAUDE.md), "Only the maintainer says yes to these").

Each step is a gate: the next one doesn't start until it passes. Where a gate can be a program it is one, because a program doesn't talk itself into anything at the end of a long run.

| # | Step | Gate |
|---|---|---|
| 0 | Track the steps | a task list with one item per step below, one in progress at a time |
| 1 | Claim it | not already `in-progress`; on an unattended run, `check_issue_ready.py` exits 0 |
| 2 | Read it | the body, every comment, and each issue it links |
| 3 | Build it | on a branch from a fresh `origin/main`; tests written docstring first |
| 4 | Local gates | the suite, and the extra runs the change calls for |
| 5 | Audit the diff | `/branch-audit`: no critical or high finding |
| 6 | Notes and docs | `changes/<branch>.md`, and the docs the change touches |
| 7 | Commit and open the PR | named files staged; the template filled in; previews attached |
| 8 | CI on the head commit | `check_pr_ready.py --ci-only` exits 0 |
| 9 | Show it working | the Verification section holds real output |
| 10 | Ready | `check_pr_ready.py` exits 0; then report, and stop |

Several issues at once: steps 1, 2 and 10 run for each, and steps 3 to 9 once, as one pull request. Batch only issues that touch the same code, and no more than two.

## 1. Claim it

```bash
gh issue view <n> --json state,labels,assignees
```

- **Stop if it is labelled `in-progress`:** another agent has it. Ask which of you continues.
- **Stop if it is labelled `needs-decision`, `needs-grooming` or `human-gated`:** it isn't an agent's turn ([CLAUDE.md](../../../CLAUDE.md), "Labels say whose turn it is"). Say what the label is waiting for.
- **With the maintainer in the conversation,** an issue without `build-ready` is fine: questions are cheap, so ask them.
- **With nobody to ask** (an overnight run, a scheduled run), the issue must pass the gate first:

  ```bash
  python3 .github/scripts/check_issue_ready.py <n>
  ```

  A refusal costs one slot. Building from a description with an open question in it costs the slot and leaves a pull request someone has to take apart.

Then claim it, so a parallel agent sees it is taken:

```bash
gh issue edit <n> --add-label in-progress
gh issue comment <n> --body "Working on this on branch \`<type>/<short-name>\`."
```

## 2. Read it

Read the whole issue before any code: the body, **every comment oldest first**, and each issue or pull request it links. Comments change requirements, and the body is often older than they are. If two parts disagree and the maintainer is there, ask. If nobody is, take the most conservative reading and say so in the pull request, under a heading the maintainer can't miss.

A question the code can answer is yours to answer. Only a real product decision goes to the maintainer.

## 3. Build it

```bash
git fetch origin && git switch -c <type>/<short-name> origin/main
```

- **The branch name** starts with the Conventional Commits type the title will have (`fix/`, `feat/`, `docs/`, `ci/`…). The notes file is named after what follows the slash.
- **Only this issue.** No refactor on the way past, no unrelated fix. Something else worth doing becomes its own issue (`gh issue create`), filed before the pull request opens, never an "out of scope" list in the description.
- **Tests come with the change,** written docstring first (the `docstring-first-tests` skill). A bug fix starts with a test that fails for the bug's reason.
- **Never weaken a guard to get through** ([CLAUDE.md](../../../CLAUDE.md), "Guards only tighten").
- **Commit as you go** when the work has parts: each commit passes on its own.

## 4. Local gates

The suite, always:

```bash
uv run pytest -q -n auto          # about 3 minutes
```

And what the change calls for ([CONTRIBUTING.md](../../../CONTRIBUTING.md) has each in full):

| The change touches | Also run |
|---|---|
| `src/switchboard/web/static/` | `uv run pytest -m e2e tests/e2e`, then `uv run python docs/media/ui_shots.py --out DIR` |
| CLI output | `uv run python docs/media/cli_shots.py --out DIR` |
| `Dockerfile`, `deploy/` | `uv run pytest -m image tests/image` (Docker) |
| the remote link (`src/switchboard/remote/`) | `uv run pytest -m twohost tests/twohost` (Docker) |
| only Markdown | `uv run pytest -q tests/unit/test_tree_scan.py` is enough |
| timing, processes or sockets in a test | the new test 20 times, and once under `-n auto` (below) |

```bash
for i in $(seq 1 20); do uv run pytest -q -p no:cacheprovider <test> || break; done
```

A test that fails one run in twenty isn't finished. This repo's flaky tests have all been timing assumptions in the test (#20, #37, #48), and "retry it" has never been the fix.

A failure in a test the change can't have touched: run that test alone. If it passes alone, say which one and carry on, and CI decides. If it fails alone, it is yours until shown otherwise.

## 5. Audit the diff

Run `/branch-audit`. It reads only what this branch adds, against the bug patterns this codebase has. Critical and high findings are fixed, and steps 4 and 5 run again. Medium and low are fixed too on an unattended run, or listed in the pull request when the fix would leave the issue's scope.

## 6. Notes and docs

- **`changes/<name>.md`** for anything a user would notice, under the CHANGELOG's headings ([changes/README.md](../../../changes/README.md)). Never CHANGELOG.md.
- **The docs the change touches:** the README, `docs/USAGE.md`, and `docs/DESIGN.md` when the design moved. A decision made along the way goes in the docs, not only in the pull request.

## 7. Commit and open the PR

- **Stage files by name.** Never `git add .` or `-A`: that is how databases, logs and scratch files get in.
- **Never `--no-verify`,** and never amend or force-push a branch someone may have read. A fix is a new commit.
- **The title** is a Conventional Commits one, and becomes the one commit on main: `fix(web): keep remote names visible in the sidebar`.
- **The description** follows [the template](../../../.github/pull_request_template.md): what and why in plain words, what it does, previews, verification, the checklist items that apply (delete the rest), and `Closes #<n>`.
- **Previews** for anything a person sees, made from a throwaway home and pushed to `design-assets` under `pr-<number>/`, linked by commit SHA ([CLAUDE.md](../../../CLAUDE.md), "Every pull request shows what it changes"). The number isn't known until the pull request exists: open it, push the previews, then edit the description.

```bash
git push -u origin <branch>
gh pr create --title "<title>" --body-file <file>
```

## 8. CI on the head commit

Wait for the checks **on the commit you pushed**. A green check from the commit before proves nothing about this one, and for the first seconds after a push there is no run at all.

```bash
for i in $(seq 1 40); do                      # 40 minutes at most; never `while true`
  python3 .github/scripts/check_pr_ready.py --ci-only <pr>; rc=$?
  [ "$rc" -ne 2 ] && break                    # 0 green, 1 red; 2 is still running, or not started
  sleep 60
done
```

Where the harness watches CI for you (it wakes you when a check finishes), use that and run the command once at the end.

**On red:** read the failing job's log (`gh run view <run> --log-failed`). Fix it, pass steps 4 and 5 again, commit, push, and wait again. If the failing test is one the diff can't have touched and it passes alone locally, re-run that job once (`gh run rerun <run> --failed`) and say so in the pull request. A second failure is real.

## 9. Show it working

The description's **Verification** section holds evidence from the finished change, not a summary of it:

- **Something a person sees:** the previews, before and after, light and dark, and the phone layout when it changed.
- **Anything else:** the command and what it printed. A bug fix shows the failing case passing: the new test's name and its output, or the CLI run that used to go wrong.
- **"It's only the backend" is rarely true.** A status, a notice, a report line, a log line an operator reads: find where it shows and show it.

Evidence comes from running the real thing against a test-mode broker in a temp home, never the maintainer's running broker or their harness config.

## 10. Ready

```bash
python3 .github/scripts/check_pr_ready.py <pr>
```

Exit 0, on the head commit, in this turn. Then:

```bash
gh issue edit <n> --remove-label in-progress
```

and report: the pull request's link, what it does in two sentences, the evidence, anything assumed, and the post-merge ops it lists. **Don't merge.** After the maintainer does, `/close-out <n>` finishes the job.

If the gate refuses and you can't fix it, say which gate and why. A pull request that isn't ready is a draft (`gh pr ready --undo`), with the reason in a comment.

## What goes wrong

| Mistake | Why it matters |
|---|---|
| Reporting "CI is green" off an earlier commit | The pushed commit was never tested. Step 8 reads the head commit's checks. |
| "Verified" in your own words | A summary is a claim. Step 9 wants output or a picture. |
| Reading only the issue's body | The comments are where the requirement changed. |
| Guessing at an ambiguity with the maintainer right there | A question takes two minutes. A wrong build takes a review, a revert and a rebuild. |
| One commit at the end for work with parts | Nothing between the first line and the last ever passed on its own. |
| `git add .` | Scratch files, databases and local details reach a public repo. |
| Fixing something else on the way past | The pull request stops being reviewable as one thing. File an issue. |
| Lowering `COVERAGE_FLOOR`, skipping a test, adding a fake name to the tree scan | A guard that moves when pushed isn't a guard. Ask. |
| Merging because the checks passed | Passing checks are what makes it reviewable, not approved. |

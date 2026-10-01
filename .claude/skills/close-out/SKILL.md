---
name: close-out
description: Finish an issue after its pull request merged - confirm the merge, surface the pull request's unticked post-merge ops, check the issue closed, and delete the local branch and worktree. Use when the maintainer says a pull request has been merged.
argument-hint: "<issue or pr> [...]"
---

# Close out

`work-on-an-issue` stops at ready for review, because the maintainer's review is the last gate. This picks up after the merge.

## 1. Confirm it merged

```bash
gh pr view <pr> --json state,mergeCommit,mergedAt,headRefName,body,closingIssuesReferences
```

`state` must be `MERGED`. If it isn't, stop and say so: nothing below applies to an open pull request.

## 2. Post-merge ops, before anything is called done

Read the merged description's **Post-merge ops** section.

- **Nothing unticked** (or no section): go on.
- **Something unticked:** list each item back to the maintainer, word for word, and for each either
  - they confirm it is done, and you tick it in the description (`gh pr edit <pr> --body-file <file>`), or
  - they defer it with a reason, which you record in a comment on the issue.

Never tick an item for the maintainer, and never do one that is theirs: restarting their broker, upgrading their other machines, cutting a release, changing a repo setting ([CLAUDE.md](../../../CLAUDE.md), "Only the maintainer says yes to these"). An op you were asked to do is ticked only after you have checked its result, not after you ran its command.

## 3. The issue

`Closes #<n>` closes the issue at the merge. Check:

```bash
gh issue view <n> --json state,labels
```

- **Still open** (the description had no `Closes`): comment with the merge commit and close it, unless a deferred op keeps it open.
- **Labels:** remove `in-progress` and `build-ready` if they are still there.

## 4. The local checkout

```bash
git switch main && git pull --prune
git worktree remove .worktrees/<name>      # if the work had one
git branch -d <branch>
```

- **`git branch -d` refuses after a squash merge,** because the branch's commits aren't on main. Check the pull request is merged and the branch has no commit that was never pushed (`git log origin/<branch>..<branch>` printed nothing before the prune, or `gh pr view` shows the same head commit), then `-D`.
- **Uncommitted work in the checkout:** stop and ask. Never stash or discard someone's changes to tidy up.

## Report

The merge commit, the issue's state, each post-merge op as done, deferred or the maintainer's, and what was deleted locally.

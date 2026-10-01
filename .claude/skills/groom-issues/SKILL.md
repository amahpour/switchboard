---
name: groom-issues
description: Triage open issues into buildable, needs-a-decision or not-a-build, check each buildable one's claims against the code, and stamp and label it build-ready so an unattended agent may take it. Use before queueing anything for an overnight run, when the maintainer has answered questions on an issue, or when asked to groom or triage issues.
argument-hint: "<issue> [issue ...]"
---

# Groom issues

An issue that reads well isn't one an agent can build alone. This skill is the step between *someone wrote it down* and *an agent with nobody to ask can build it at 2am*.

Every issue leaves in exactly one of three states:

| State | Means | What happens |
|---|---|---|
| **Buildable** | Every decision is made, and every claim about the code is checked. | Rewrite the body, stamp it, label it `build-ready`. |
| **Needs a decision** | A product decision is still open. | One comment with the questions, and the `needs-decision` label. |
| **Not a build** | A spike, something deferred, or work whose deliverable is an action only the maintainer takes. | The first line says so, and the label is `human-gated` (or none, for a spike). |

An issue can come back for a decision three times. That costs less than one wrong build.

## The bar

A description is ready for an unattended agent only when all of these hold:

1. **No open question,** in the body or in a comment. One pending decision means *needs a decision*.
2. **No unchecked claim about the code.** "The field already exists" was looked up, not assumed. People describe the program they believe exists.
3. **Acceptance criteria that can be tested:** assertions, not "works well".
4. **Named code:** the files, functions and tests the work touches, as repo paths (`src/switchboard/delivery/engine.py`). A file the work creates says `new` on its line.
5. **Nothing only the maintainer may do** ([CLAUDE.md](../../../CLAUDE.md), "Only the maintainer says yes to these"). Split it: the code half can be buildable, and the action half is its own `human-gated` issue.
6. **No collision left open.** If two issues need the same new thing, one of them says it builds it.
7. **Every recorded decision quotes the maintainer,** in quotation marks, with the date. If you can't quote it, you don't have it. A comment that stops mid-sentence, or answers two of three questions, settled only what it said: never finish someone's thought for them, and never infer the half of a rule they didn't state.
8. **Every dependency is a line the gate can read:** `Blocked by #41.` The gate refuses the issue while #41 is open. Prose may explain why. The line is what makes it visible.

## The loop

### 1. Read the whole thread, oldest first

The body, then every comment. Later comments reverse earlier ones, and the body is often the oldest and the least true.

### 2. Split what's open into two lists

| Kind | Example | Who answers |
|---|---|---|
| **A decision** only a person can make | "Should Auto mode get its own label?" | The maintainer |
| **A fact** the repo knows | "Does the hook send this field on every event?" | You, before you write anything |

Never send a fact question to the maintainer. Look it up, and write the answer into the issue.

### 3. Answer every fact question first

Grep, read the code, run the test, read the recorded payloads in `tests/fixtures/payloads/`. This step often dissolves the decision: the field already exists, the proposed approach can't work, the issue is half done.

If you can't measure something, say so. A made-up number is worse than none.

### 4. Check the body against the code, claim by claim

For each claim the description makes about the code, record one of:

- **Confirmed,** with the current `path:line`;
- **Moved,** from where to where;
- **False,** and what is true.

Keep the list in your notes for the report. This is the step that makes the stamp mean something, and it is the easy one to skip, because skipping it looks the same from outside. `check_issue_ready.py` can't do it for you: it checks that a stamp exists and that the named files haven't changed, and it has never read the code the description describes.

### 5. Act on the verdict

**Buildable.** Rewrite the body in place: an agent reads from the top and builds what it reads, so decisions appended under a stale body get the stale spec built. Then, in this order:

```bash
git fetch origin && git rev-parse --short origin/main
```

Put the stamp on its own line near the top, with that commit and today's date:

```
Build-ready: verified against origin/main @ <sha> on <YYYY-MM-DD>.
```

```bash
gh issue edit <n> --body-file <file>
gh issue edit <n> --add-label build-ready --remove-label needs-grooming
python3 .github/scripts/check_issue_ready.py <n>      # the same program the queue runs
```

Exit 0, or it isn't done.

**Needs a decision.** One comment, then the label:

1. Lead with what your checking changed, if it changed anything.
2. Answer each fact question that was asked, with the evidence.
3. State an assumption where a default is defensible ("I'll build X unless you say otherwise") and don't ask.
4. Number the real decisions, each with a recommendation and what being wrong would cost. The maintainer answers by number.
5. End with what is now settled, so the thread doesn't reopen it.

```bash
gh issue comment <n> --body-file <file>
gh issue edit <n> --add-label needs-decision --remove-label build-ready
```

**Not a build.** Rewrite the first line to say so, and label it `human-gated` when its deliverable is the maintainer's action.

## Keep the label true

- **Take `build-ready` off when the body changes in substance,** or re-check and re-stamp. A label left on an edited body turns a gate into a rubber stamp.
- **A stale stamp** (the gate says a named file changed) means re-check those claims and re-stamp. `--allow-stale` is for a description that names symbols, not lines, and is used per issue, on purpose.
- **Never widen scope for the maintainer.** "Defer it" means it is deferred, not replanned smaller.

## Report

A table: the issue, its state, and one line of why. Then the claims that were moved or false, since those are what would have gone wrong.

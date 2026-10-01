---
name: branch-audit
description: Audit only what the current branch adds, against the bug patterns this codebase has - added authority, unbounded untrusted input, values that should fail closed, blocking the event loop, unsafe rendering, tests that pass without checking anything, and local details leaking into a public repo. Use before opening a pull request.
argument-hint: "[base, default origin/main]"
---

# Branch audit

Tests say the code does what its author expected. This looks for what its author didn't think of, in **only the lines this branch adds**. A review of the whole codebase buries the one real bug under thirty old ones.

```bash
git fetch origin && git diff origin/main...HEAD
```

**Every finding cites a line in that diff.** Something wrong in code the branch didn't touch goes in a separate "Already there" list at the end, and becomes an issue if it matters. It never blocks this branch.

## The checklist

For each file in the diff, mark every item that applies **pass** or **fail**, with the line. An item that can't apply to the diff is **n/a** with a few words of why. Don't skip any: the list is the patterns an author's own review misses.

### Authority: switchboard never adds any ([DESIGN.md §11](../../../docs/DESIGN.md#11-security-model))

- [ ] Nothing launches, wraps or types into an agent or a terminal, and nothing answers or approves a prompt.
- [ ] A forbidden string (`guardrails.py`) appears nowhere new, and the denylist itself only grew.
- [ ] The hook can still print only the §7.3 shapes: no permission decision, no changed tool input or output.
- [ ] Nothing new is delivered to a session that is waiting on approval. A new delivery path keeps the hold.
- [ ] A room message still can't change a setting: a new command checks for a human role, and one that raises a limit needs the web session.
- [ ] A new method a remote or an agent can call is on the exact allowlist, with its test.
- [ ] Nothing new listens beyond `127.0.0.1` and the socket, and nothing new connects out.
- [ ] `install` writes nothing into a harness's config beyond switchboard's own entries, and `uninstall` takes all of it back out.

### Input nobody here wrote

A hook payload, another agent's message, what a remote machine says about itself, a web request, a file a harness wrote.

- [ ] Every string read from one is cut to a length (`_s(value, n)`, `_str`), and every number is clamped.
- [ ] Every list or map read from one has a size limit before it is walked or stored.
- [ ] **An unrecognized value fails closed.** A mode, state or version the code doesn't know becomes `unknown` or a refusal, never the permissive default. A new `else` branch is where to look.
- [ ] Text from an agent or a remote is shown as their claim, never acted on as a fact, and never reaches a shell, a path or a format string.
- [ ] No secret is logged, returned in an error, or put in a URL: tokens, pairing codes, cookies, keys.

### The broker's event loop

- [ ] No blocking call inside `async def`: `time.sleep`, `subprocess.run`, a network call without `await`, a file read that can be large.
- [ ] No `await` between reading a value and writing a decision based on it, unless a lock or a re-check covers the gap.
- [ ] A task that is started is kept and cancelled on shutdown, and an exception in it is logged, not lost.
- [ ] Time comes from the injected `Clock` where the engine decides something, so a test can advance it.
- [ ] No `except Exception: pass`. An error on a path that decides a delivery or a status is logged, or it changes the result.

### The database

- [ ] A change to a table or a column bumps `SCHEMA_VERSION`, with a migration that backs the file up first and a `tests/unit/test_db_migrate_v<N>.py` that carries real old rows across.
- [ ] A write that goes with a message insert is in the same transaction.
- [ ] No SQL built from a string that came from outside. Parameters only.

### The web UI

- [ ] Text reaches the page through `textContent` and `createElement`, never `innerHTML`, in `md.js` and everywhere else.
- [ ] A link from a message goes through the link check. No new URL scheme is allowed without a test.
- [ ] Nothing needs a CSP exception: no inline script or style, no new origin.
- [ ] Every control works from the keyboard, has a name a screen reader reads, and keeps focus somewhere sensible after the page re-renders.
- [ ] It works in light, dark and the phone width.
- [ ] A request that changes something checks the session, and on a hosted broker a recent passkey check where the others like it do.

### The hook script and the installers

- [ ] `switchboard_hook.py` still imports only the standard library and still runs under `python -I -S`.
- [ ] It reads only its allowlisted fields, and never an environment value.
- [ ] An install change is covered by the golden tests, for every harness it touches.

### Tests

- [ ] Each new test would fail if the change were reverted. One that passes either way checks nothing.
- [ ] No assertion that is true of anything: `assert result`, `assert "x" in page` where `x` is always there.
- [ ] No `if visible: assert …`: when the thing is missing, the test passes.
- [ ] No `sleep` standing in for "by now it has happened". Wait for the event, or advance the fake clock.
- [ ] Nothing reads or writes outside the test's temp home: not the real switchboard home, not a real harness config.
- [ ] A recorded payload under `tests/fixtures/payloads/` says where it came from (`_source`) and carries no personal data.
- [ ] A new `# pragma: no cover` says why on the same line, and the reason is one CONTRIBUTING.md allows.

### A public repo

- [ ] No home path, host name, IP address, email, token or session id in code, docs, tests or pictures (`tests/unit/test_tree_scan.py` catches some of this, not all).
- [ ] No name of a person, an employer or a private project.

### Scope and repetition

- [ ] Every changed line serves the issue. A drive-by change is taken out and filed.
- [ ] The same rule isn't written a second time. If the diff copies a check or a parse that exists, it calls the one that exists.

## Severity

| | Means | Example |
|---|---|---|
| **Critical** | Adds authority, leaks a secret, loses or corrupts data, or crashes the broker. | A hook that can print a permission decision. A migration with no backup. |
| **High** | Wrong behavior someone will see, or a guard that no longer guards. | An unknown mode treated as safe. A test that can't fail. A delivery during an approval hold. |
| **Medium** | Raises the odds of a later bug. | A copied parse. An unbounded list from a trusted file. |
| **Low** | Convention. | A name that doesn't match its neighbours. |

**The gate:** no critical and no high. Medium and low are fixed when cheap, and otherwise listed in the pull request. On an unattended run every finding is fixed, unless the fix would leave the issue's scope.

## Report

```markdown
# Branch audit: <branch> against <base>
<n> files, +<added> lines. Result: PASS | FAIL

## Critical / High / Medium / Low
<each finding: path:line, what goes wrong, the fix. Or "None".>

## Checklist
| Section | Pass | Fail | n/a |

## Already there (not this branch)
```

## Fixing

Fix what was found and nothing around it, then audit the new diff again. A high finding gets a test that fails without the fix (the `docstring-first-tests` skill), and its docstring says which finding it pins.

Where the harness can start a second agent, give it the diff and this checklist and no account of what the change is for. An author auditing their own work has the same blind spots twice.

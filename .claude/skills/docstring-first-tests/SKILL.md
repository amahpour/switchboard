---
name: docstring-first-tests
description: Write a test's docstring before its body - what behavior it checks, which bug it would catch, and how. Use when adding or changing any test in this repo (pytest unit and integration tests, the node tests of the web UI, the Playwright e2e tests).
---

# Docstring-first tests

A test written after the code tends to restate the code. It passes, and it would pass with the bug back in. Writing down what the test is for, before writing the test, is the cheapest way to notice that.

## The rule

Before the first line of the body, the docstring answers three questions:

1. **What** behavior does this check? Something observable: a status, a delivered batch, a line of output, a refused request.
2. **Why:** what bug or regression makes it fail? Name the issue or the audit finding when there is one.
3. **How:** what is set up, what is done, and what is asserted.

Scale it to the test. A test whose name already says it (`test_a_closed_issue_is_refused`) and whose body is a line or two needs no docstring, and for most unit tests one sentence of what is enough ("Pause, hold and an empty budget each hold a wake back; lifting them delivers it."). A test with setup, timing, a stand-in harness or a browser gets all three.

```python
def test_a_local_commit_that_was_never_pushed_refuses_however_green_the_old_one_is() -> None:
    """A pull request with passing checks is refused when the local HEAD isn't its head commit.

    Why: the green checks belong to the commit before. The fix made locally was never pushed,
    so nothing has tested it, and "CI is green" would be a statement about other code.

    How: a pull request whose required checks all passed on its head, evaluated with a
    different local commit. The verdict must name both commits.
    """
```

## Before writing the body, check the docstring

- **It names behavior, not structure.** "The member's status is `busy`", not "`_set_status` is called".
- **It says what breaking looks like.** If you can't say which bug would make it fail, it probably can't fail.
- **The assertion is specific.** "Exactly one batch, with ids 4 and 5", not "something was delivered".

## Then prove it can fail

Run it against the code without the change (stash the fix, or flip the condition). A new test that passes before the fix is checking something else. For a guard, break the guard and watch the test go red. Say in the pull request that you did.

## Patterns that pass without checking

| Pattern | What happens | Write this |
|---|---|---|
| `assert result` | True for any non-empty value. | The value: `assert result == [...]`. |
| `if row: assert row.status == "idle"` | No row, no assertion, a pass. | `assert row is not None` first, or unpack: `[row] = rows`. |
| `time.sleep(0.5)` then assert | Passes on a fast machine, fails in CI, or the reverse. | Wait for the event, or advance the fake clock. |
| `assert "error" not in output` | Also true when the command printed nothing at all. | Assert what it should print. |
| A fake that returns what the code expects | The test checks the fake. | A recorded payload from `tests/fixtures/payloads/`, or the stand-in harness. |
| Only the happy path | The refusal, the timeout and the unknown value are where the bugs are. | One test per way it should say no. |
| `expect(locator).to_be_visible()` as the whole test | The page rendered. | What the page did: the request sent, the text that changed, where focus went. |

## This repo's own rules

- **Time:** engine tests advance the injected clock. Nothing waits on a wall clock to prove an order of events.
- **Homes:** every test runs in a temp switchboard home. None touches a real one, or a real harness config.
- **Fixtures:** a recorded payload keeps its `_source`, and a derived one says what was changed (`_derived`).
- **Markers:** `live`, `perf`, `e2e`, `twohost` and `image` are opt-in ([CONTRIBUTING.md](../../../CONTRIBUTING.md)). A test that needs a real agent CLI is `live`, never in the default suite.
- **Coverage** is a floor that only rises. Cover code with a test that checks what it does, not one that only runs it.

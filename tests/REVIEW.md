# Review rules: tests

Applies to `tests/`. The root [REVIEW.md](../REVIEW.md) applies too.

## Flag

- **A test that touches a real home.** It owns a temp home (`tmp_home`), never `~/.switchboard` or a harness's real config. Child processes get an allowlisted environment (`child_env`).
- **A temp home made and not removed.** Whatever calls `make_tmp_home()` (a fixture, or a helper like the meeting tests' `World`) removes it when it ends; `no_homes_left_behind` fails the run otherwise. A meeting `World` that only stopped its broker left 109 homes a run (#919).
- **A harness payload written from memory.** What a harness sends is recorded: a sanitized payload in `tests/fixtures/payloads/<harness>/` with its `_source` (CLI and version), and `test_fixtures_scan.py` checks it for personal data.
- **A test with no stated reason.** Its name or a docstring says the behavior and the bug that would fail it. A new test is seen to fail without the change.
- **A sleep, or a fixed wait for a process or socket.** Wait for the event (the ready line, the socket) with a bound that holds under load (#857). A test with timing, processes or sockets runs 20 times alone and once under `-n auto`.
- **A test in the wrong place.** A pure rule in `unit/`, a broker or process in `integration/`, the page in `e2e/`.
- **A failing test suppressed to get green, a weakened marker, or a new skip that removes required coverage.** Gated live, image and e2e tests and the local PowerShell skips are by design. Coverage only rises (`COVERAGE_FLOOR`). Many new tests: `tests/.test_durations` regenerated.
- **A real name or host in a fixture.** People are alice and bob; hosts are `example.com`.

## Skip

- `tests/.test_durations`: generated.
- Test style that lint accepts.

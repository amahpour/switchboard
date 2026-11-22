# Review rules: tests

Applies to `tests/`. The root [REVIEW.md](../REVIEW.md) applies too.

## Flag

- **A test that touches a real home.** It owns a temp home (`tmp_home`), never `~/.switchboard` or a harness's real config. Child processes get an allowlisted environment (`child_env`).
- **A temp home made and not removed.** Whatever calls `make_tmp_home()` (a fixture, or a helper like the meeting tests' `World`) removes it when it ends; `no_homes_left_behind` fails the run otherwise. A meeting `World` that only stopped its broker left 109 homes a run (#919).
- **A harness payload written from memory.** What a harness sends is recorded: a sanitized payload in `tests/fixtures/payloads/<harness>/` with its `_source` (CLI and version), and `test_fixtures_scan.py` checks it for personal data.
- **A test with no stated reason.** Its name or a docstring says the behavior and the bug that would fail it. A new test is seen to fail without the change.
- **A child process given a temp home without its lock.** A test that starts a process using a `make_tmp_home()` home passes it `pass_fds=tmp_homes.lock_fds_for(home)`, as `SubprocBroker` does, so a child that outlives pytest keeps its home from the session-start sweep (#922). A daemonized broker holds `run/broker.lock` instead, which the sweep takes too: a check of a home's use that skips it, reads a home by path after locking it, or ignores an error while looking inside is the bug #922's review found. A home a fixture removes goes through `tmp_homes.remove_home()`, which removes it in place, the marker and the lock file last, so a removal stopped part way leaves the home under its own name, where the leak check sees it and the next sweep takes it; a rename to another name (the `yk-gone-*` tombstones of #922's second round) hid it from both. Nothing is chosen by its name alone. A home with `logs/broker.out` and no `run/broker.lock` is kept: `switchboard start` makes the one before its daemon makes the other. One sweep runs at a time, under the root's `yk-sweep.lock`. A test that reads a child's pipe uses `select` and `os.read` with a deadline, never a buffered read in a thread (closing the stream then blocks), and kills the child's process group and reaps it before closing a pipe. The sweep never reads a home's age, a pid or `/proc`: a running broker leaves its folder's mtime old, the maker isn't the only user, and `/proc/<pid>/fd` is refused for ssh-agent (#920's reviews).
- **A sleep, or a fixed wait for a process or socket.** Wait for the event (the ready line, the socket) with a bound that holds under load (#857). A test with timing, processes or sockets runs 20 times alone and once under `-n auto`.
- **A test in the wrong place.** A pure rule in `unit/`, a broker or process in `integration/`, the page in `e2e/`.
- **A failing test suppressed to get green, a weakened marker, or a new skip that removes required coverage.** Gated live, image and e2e tests and the local PowerShell skips are by design. Coverage only rises (`COVERAGE_FLOOR`). Many new tests: `tests/.test_durations` regenerated.
- **A real name or host in a fixture.** People are alice and bob; hosts are `example.com`.

## Skip

- `tests/.test_durations`: generated.
- Test style that lint accepts.

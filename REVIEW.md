# Review rules: whole repository

Read this and the nearest `REVIEW.md` of each changed file (`plugins/`, `deploy/joinly/`, `src/switchboard/web/`, `tests/`). [CLAUDE.md](CLAUDE.md) has the full rules; this is what reviews here keep catching or missing.

## Levels

Start each finding's title with its level, so the author and whoever merges know what blocks:

- **Important:** a bug a user would hit, data loss, a change to the security model, a guard that loosens, or any rule under "Flag" below that the change breaks, such as a test that passes without the change or a doc that says something the code doesn't do. It blocks the merge.
- **Nit:** optional polish: wording that is right but could read better, naming, a comment, a test's setup that works. It doesn't block.
- **Pre-existing:** there before this change. It doesn't block.

The author contests a Nit it won't fix, and a Pre-existing finding, while its card is still raised, with the reason (for a Pre-existing one, where the pull request lists it); for a card it has already conceded, it says the same in the room. Then drop it, or defer it to an issue, instead of insisting: insist only on an Important one. Confirm a fixed Nit when the fix is sound, rather than reopen it for more polish. Only the raiser or a person can drop, defer or confirm a card, so a Nit holds up the board until its raiser settles it. After two rounds, drop or defer your Nit and Pre-existing cards that are still open, and confirm the sound fixes, so only Important cards stay open.

A round is one review of a new head. After two rounds, raise only Important findings, and check the fixes of the ones you raised. Past round two, one finding in four was a bug a user could hit; the rest were in code the review itself had asked for, wording and test setup. #896 ran 13 rounds, and rounds 8 to 13 were nearly all fixes of earlier fixes.

## Flag

- **A review fix that adds a new mechanism:** a lock, a migration, a file format, a new state. The design is moving under the review: it is Important, and the maintainer decides it before it is built. #924's third and fourth rounds added a sweep lock and a removal marker, and its review ran to seven rounds.
- **A change to the security model** ([DESIGN.md §11](docs/DESIGN.md#11-security-model)): something that approves, launches or types into an agent, or delivers to one waiting on an approval; a room message that changes a setting; a hook or plugin that decides a permission (a report-only one outputs no decision); a bind beyond `127.0.0.1` that skips the `--listen` plus `--public-url` pair; a hook answer outside `broker/hook_replies.py`; a secret in a log, a URL or an error. It is a design change and needs an issue.
- **A guard that loosens.** The coverage floor, `guardrails.py`, the tree scan, an allowlist, the CSP, an opt-in test marker may only tighten. A skipped or deleted failing test, or a new `# pragma: no cover`, needs the maintainer's yes quoted in the pull request.
- **A new test never seen to fail.** Ask for the failing run without the change. A test that passes either way checks nothing.
- **A sleep that means "by now", or a timing assumption.** Rule tests advance the `FakeClock`; process tests wait for the event. Every flaky test so far was this (#20, #37, #48; the broker fixtures of #857 waited a fixed time).
- **A runtime import whose package is not installed with switchboard.** 1.9.0 shipped without `httpx`: the tests passed on the dev group and the image could not start the talker (#848). `tests/unit/test_runtime_dependencies.py` scans every import, function-local ones included, against the runtime-only closure. Look for what it cannot see: an import made through `importlib` or `__import__`, and a new package.
- **A value from outside with no limit.** A hook payload, a message, a web request: cut to a length, clamped, a claim. An unknown value fails closed, never to the permissive default.
- **SQL outside `store.py`; delivery policy outside `delivery/rules.py`; an `async def` that blocks.**
- **Docs left saying the old thing.** The README, `docs/USAGE.md`, the feature's guide, DESIGN.md, and a `changes/<name>.md` for what a user would notice. Never `CHANGELOG.md`.
- **A home path, host name, IP address, email, token or real person's name** (tests use alice and bob).

## Skip

- What `scripts/lint.py` and CI already enforce: formatting, types, ShellCheck, the tree scan, the docs build.
- `CHANGELOG.md`, which a release writes. Generated vendor files are skipped line by line, not as a change: see `src/switchboard/web/REVIEW.md`.

# Review rules: whole repository

Read this and the nearest `REVIEW.md` of each changed file (`plugins/`, `deploy/joinly/`, `src/switchboard/web/static/`, `tests/`). [CLAUDE.md](CLAUDE.md) has the full rules; this is what reviews here keep catching or missing.

## Flag

- **A change to the security model** ([DESIGN.md §11](docs/DESIGN.md#11-security-model)): something that approves, launches, types into an agent or delivers to one waiting on an approval; a hook answer outside `broker/hook_replies.py`; a listener beyond `127.0.0.1`; a secret in a log, a URL or an error. It is a design change and needs an issue.
- **A guard that loosens.** The coverage floor, `guardrails.py`, the tree scan, an allowlist, the CSP, an opt-in test marker may only tighten. A skipped or deleted failing test, or a new `# pragma: no cover`, needs the maintainer's yes quoted in the pull request.
- **A new test never seen to fail.** Ask for the failing run without the change. A test that passes either way checks nothing.
- **A sleep that means "by now", or a timing assumption.** Rule tests advance the `FakeClock`; process tests wait for the event. Every flaky test so far was this (#20, #37, #48; the broker fixtures of #857 waited a fixed time).
- **A runtime import whose package is not in `[project] dependencies`.** 1.9.0 shipped without `httpx`: the tests passed on the dev group and the image could not start the talker (#848). `tests/unit/test_runtime_dependencies.py` catches the plain case; look at a lazy import and at a new package.
- **A value from outside with no limit.** A hook payload, a message, a web request: cut to a length, clamped, a claim. An unknown value fails closed, never to the permissive default.
- **SQL outside `store.py`; policy outside `delivery/rules.py`; an `async def` that blocks.**
- **Docs left saying the old thing.** The README, `docs/USAGE.md`, the feature's guide, DESIGN.md, and a `changes/<name>.md` for what a user would notice. Never `CHANGELOG.md`.
- **A home path, host name, IP address, email, token or real person's name** (tests use alice and bob).

## Skip

- What `scripts/lint.py` and CI already enforce: formatting, types, ShellCheck, the tree scan, the docs build.
- Files under `src/switchboard/web/static/vendor/` (vendored board and Mermaid files), and `CHANGELOG.md`, which a release writes.

<!--
  Open with one or two sentences in plain words: what was wrong or missing, and what this does
  about it. No file paths up here. A release PR (release.py --open-pr) doesn't use this template.

  Title: Conventional Commits, `fix(web): keep remote names visible in the sidebar`. PRs are
  squash-merged, so the title is the one commit on main.

  Delete every comment, and every checklist item that doesn't apply. An unticked box reads as
  "skipped", not "doesn't apply". check_pr_ready.py refuses a PR with one left under Pre-merge.
-->

## What it does

<!-- A short list a user or a reviewer can follow: the behavior now, and how it was before. -->

## Previews

<!-- For anything a person sees (the web UI, CLI output, a docs page): before and after, light
     and dark, the phone layout when it changed. Made from a throwaway home with
     docs/media/ui_shots.py or cli_shots.py, pushed to design-assets under pr-<number>/, and
     linked by commit SHA (CLAUDE.md). Nothing visible? Say "No visible change, so no previews." -->

## Verification

<!-- The change working, as evidence: the command you ran and what it printed, in a fenced
     block. For a bug fix, the failing case passing. Not a summary in your own words.
     Then the tests added, and that you saw each fail without the change. -->

```console
$ uv run pytest -q -n auto

```

## Docs

<!-- Which docs changed with it: README, docs/USAGE.md, docs/DESIGN.md, and changes/<name>.md
     for anything a user would notice. Never CHANGELOG.md. -->

## Pre-merge checklist

<!-- Delete the groups that don't apply. Why each item exists: .github/PR_CHECKLIST.md. -->

**The database** (`src/switchboard/db.py`, a table or a column)
- [ ] `SCHEMA_VERSION` is bumped, the migration backs the file up first, and `tests/unit/test_db_migrate_v<N>.py` carries rows from the version before across.

**The hook script or an installer** (`src/switchboard/hook/`, `src/switchboard/install/`)
- [ ] The hook still imports only the standard library and prints only the DESIGN §7.3 shapes (`tests/unit/test_hook_invariants.py`, `test_guardrails_static.py`).
- [ ] `install` then `uninstall` leaves each harness's config as it was (the golden tests).

**A harness adapter** (`src/switchboard/adapters/`, what a hook or a registry reports)
- [ ] A recorded payload for the new case is in `tests/fixtures/payloads/<harness>/`, with its `_source` (the CLI and its version).
- [ ] A value the adapter doesn't recognize still fails closed, and a test says so.

**The remote link** (`src/switchboard/remote/`)
- [ ] A machine on the last release still links up, or `LINK_PROTO` is bumped and the refusal says what to upgrade.
- [ ] `uv run pytest -m twohost tests/twohost` passed locally, or in this PR's CI.

**The web UI** (`src/switchboard/web/static/`)
- [ ] `uv run pytest -m e2e tests/e2e` passed: no console error, page error or CSP violation.
- [ ] Previews above cover light, dark and the phone width.
- [ ] Every new control works from the keyboard, and focus lands somewhere sensible after a re-render.

**CLI output**
- [ ] Previews above come from `docs/media/cli_shots.py`, and the output is still plain when it isn't a terminal.

**Dependencies** (`pyproject.toml`, `uv.lock`)
- [ ] `uv sync --frozen` passes with uv 0.7.13, the version CI pins.

**Tests with timing, processes or sockets**
- [ ] Each new one passed 20 runs in a row alone and one run under `-n auto`.
- [ ] `.test_durations` is regenerated if the suite's shape changed (`uv run pytest -n auto --store-durations`).

**The image or `deploy/`**
- [ ] `uv run pytest -m image tests/image` passed. Version pins are untouched: only a release moves them.

**A workflow** (`.github/workflows/`)
- [ ] Every action is pinned by commit SHA, and untrusted text (a title, a branch name) reaches a script through the environment, never pasted into it.
- [ ] The workflow ran on this branch: `gh workflow run <file> --ref <branch>`, or this PR's own run.

**A guard** (CLAUDE.md, "Guards only tighten")
- [ ] Nothing here loosens one. If something does, the maintainer's yes is quoted above, with its date.

## Post-merge ops

<!-- What someone must do after the merge, before the issue is done. Delete what doesn't apply.
     The `close-out` skill reads this list. Items marked (maintainer) are never an agent's. -->

- [ ] (maintainer) Restart the running broker to pick this up. A schema change migrates on that start, and the broker backs its database up first.
- [ ] (maintainer) Upgrade the other machines to the next release, so both ends of a link run the same version (docs/INSTALL.md, "To upgrade").
- [ ] (maintainer) Run `switchboard install all` again after upgrading: the hook script or what an installer writes changed.
- [ ] Add or change a label, a ruleset or another repo setting this relies on.

Closes #

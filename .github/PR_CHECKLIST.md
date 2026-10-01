# Why each item is on the pull request checklist

[The template](pull_request_template.md) carries a checklist whose items apply only to some changes. Each is there because that kind of mistake is easy to make here and expensive to find later. An item with no reason gets deleted from the template, not kept "to be safe": a long checklist gets ticked without being read.

When a mistake reaches main that a one-line check would have caught, add the check here with the pull request that shows why.

## Verification, and not a summary

The description shows the change working: a command and its output, or a picture. `check_pr_ready.py` refuses a pull request without one. An agent's account of its own work is the one thing a reviewer can't check, and it reads the same whether or not the work was done.

## The database

- **A version bump, a backup, and a migration test with real old rows.** The broker migrates a home's database when it starts and refuses a schema it doesn't know (`src/switchboard/db.py`). A change with no migration stops every existing home from starting. Each version so far has its test (`tests/unit/test_db_migrate_v2.py` to `v4.py`).

## The hook script and the installers

- **Standard library only, and only the §7.3 shapes.** Harnesses run the hook as `python -I -S <copy>`, so an import that works in the checkout fails in a real session, where no test sees it. The shapes are the security model: a hook that could print a permission decision would let room text approve a prompt ([DESIGN.md §11](../docs/DESIGN.md#11-security-model)).
- **Install, then uninstall, leaves the config as it was.** The installers edit other programs' config files. Whatever one leaves behind is in someone's real setup for good.

## A harness adapter

- **A recorded payload, with where it came from.** The adapters are built on what each CLI really sends, and the CLIs change. A case written from memory of the format tests the memory. Issue #73 is the shape of it: two modes the hooks report weren't in the recordings, so they showed as unknown.
- **Unknown values fail closed.** An agent shown as "approvals on" when it isn't is the worst thing the members list can say.

## The remote link

- **The last release still links up, or the refusal says what to do.** A broker and its machines are upgraded at different times. A machine that is refused at every dial has to say why (#53), or it just looks offline.
- **The two-host tests.** They are opt-in locally and need Docker, so they are the ones that get skipped.

## The web UI

- **The e2e tests.** They fail on any console error, page error or CSP violation, which nothing else notices.
- **Light, dark and the phone.** `docs/media/ui_shots.py` makes all three from one seeded broker, so showing them costs a command. A colour or a width checked in one is unchecked in the others.
- **Keyboard and focus.** A re-render that drops focus isn't visible in a screenshot.

## CLI output

- **Plain when it isn't a terminal.** Colour is for a terminal, and off under `NO_COLOR` (`src/switchboard/colors.py`, #30). Escape codes in a pipe or a log break whatever reads them.

## Dependencies

- **`uv sync --frozen` with uv 0.7.13.** CI pins that version so a lock a newer uv would rewrite fails there and not in a release (#17).

## Tests with timing, processes or sockets

- **Twenty runs alone, one under `-n auto`.** Every flaky test fixed so far was a timing assumption in the test, and each showed up first on a slow CI runner, in someone else's pull request (#20, #37, #48).
- **`.test_durations`.** CI balances its shards from this file (#32). A big new group of tests it doesn't know about makes one shard slow.

## The image and `deploy/`

- **The image tests.** They start the image the way a platform does, behind a TLS proxy (#39). The unit tests never see the image.
- **Pins are a release's.** `release.py` moves every version pin. One moved by hand points at a version that doesn't exist yet.

## A workflow

- **Actions pinned by SHA; untrusted text through the environment.** A pull request's title is text someone else wrote. Pasted into a script, it runs.
- **The workflow ran on the branch.** A workflow that only runs after a merge gets its first test on main, where a failure blocks everyone.

## A guard

- **Nothing loosens one without the maintainer's yes, quoted.** The coverage floor, the forbidden-strings list, the tree scan, the allowlists and the CSP exist to say no. An agent that needs a guard out of its way has usually found the bug the guard is for.

## Post-merge ops

- **They are in the description, and `close-out` reads them.** What has to happen after a merge (a restart, an upgrade on another machine, a re-install) isn't in the diff, so nothing else remembers it.
- **(maintainer) items stay the maintainer's.** A running broker, another machine and a harness's real config aren't an agent's to touch ([CLAUDE.md](../CLAUDE.md), "Only the maintainer says yes to these").

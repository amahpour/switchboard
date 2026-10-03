---
name: work-on-an-issue
description: Take a GitHub issue, or a one-line task, to a pull request that is ready for the maintainer's review - branch from main, build it with its tests, run the suite, review the diff, open the pull request with evidence, see CI pass, and stop. Use when asked to work on, fix, build or implement something in this repo.
argument-hint: "<issue number, or what to do>"
---

# Work on an issue

The rules are in [CLAUDE.md](../../../CLAUDE.md); this is the order of work. It ends at a pull request the maintainer can review, never a merge.

1. **Read it whole.** `gh issue view <n> --comments`: the body, every comment, what it links. Unclear and the maintainer is here: ask. Unclear and nobody is: the most conservative reading, listed under **Assumptions** at the top of the pull request.

2. **Branch.** `git fetch origin && git switch -c <type>/<name> origin/main`, where `<type>` is the title's Conventional Commits type (`fix`, `feat`, `docs`, `ci`, `test`, `refactor`, `chore`). In a shared checkout, a worktree: `git worktree add .worktrees/<name> -b <type>/<name> origin/main`.

3. **Build it, with its tests.** Write what each test checks and why before its body, then see it fail without the change: for a bug, the test first. Only this issue; anything else becomes an issue of its own. Commit as you go, files by name, never `--no-verify`.

4. **Run lint and the suite.** `uv run python scripts/lint.py` (or `--fix` while editing), then `uv run pytest -q -n auto`. Then what the change calls for:

   | It touches | Also |
   |---|---|
   | `src/switchboard/web/static/` | `uv run pytest -m e2e tests/e2e`, and `uv run python docs/media/ui_shots.py --out <dir>` for the previews |
   | CLI output | `uv run python docs/media/cli_shots.py --out <dir>` for the previews |
   | `Dockerfile`, `deploy/` | `uv run pytest -m image tests/image` (Docker) |
   | `src/switchboard/remote/` | `uv run pytest -m twohost tests/twohost` (Docker) |
   | timing, processes or sockets in a test | that test 20 times alone: `for i in $(seq 20); do uv run pytest -q -p no:cacheprovider <test> \|\| break; done` |

   A test the change can't touch that fails under `-n auto` and passes alone: say which in the pull request and go on. One that fails alone is yours until shown otherwise.

5. **Read the diff as a reviewer** (`git diff origin/main...HEAD`), for the things tests don't catch: a value from outside with no length limit; an `else` that defaults to the permissive case; a blocking call inside `async def`; a secret in a log, an error or a URL; a test that would pass without the change; a sleep standing in for an event; a home path, a host name or a person's name. Fix what you find and nothing around it.

6. **Notes and docs.** `changes/<name>.md` for anything a user would notice (never CHANGELOG.md); the README, `docs/USAGE.md` or DESIGN.md where the change moved them.

7. **Open the pull request.** `git push -u origin <branch>`, then `gh pr create` with the title and [the template](../../../.github/pull_request_template.md) filled in: what it does, the previews (pushed to `design-assets` under `pr-<number>/`, or a branch of your fork, and linked by commit SHA), the change working as a command and its output or a picture, the docs, `Closes #<n>`. The number comes with the pull request, so push the previews after opening it and edit the description.

8. **Watch CI.** `gh pr checks <pr> --watch`. On red: `gh run view <run> --log-failed`, fix with a new commit, push, watch again. A test the diff can't have touched, passing alone: re-run that job once (`gh run rerun <run> --failed`) and say so; a second failure is real.

9. **Stop.** Report the link, what it does in two sentences, the evidence, and any assumption. Don't merge. After the maintainer merges: `git worktree remove .worktrees/<name>` if there was one, and `git branch -d <branch>`.

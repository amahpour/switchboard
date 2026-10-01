<!-- Open with one or two sentences: what was wrong or missing, and what this does about it.
     Title: Conventional Commits (`fix(web): …`); it becomes the one squash commit on main.
     Delete the comments, and a section that doesn't apply. CLAUDE.md has the rules. -->

## What it does

<!-- The behavior now, and how it was before. -->

## Previews

<!-- Anything a person sees: before and after, light and dark, the phone when it changed, from a
     throwaway home (docs/media/ui_shots.py, cli_shots.py), on design-assets under pr-<number>/
     or a branch of your fork, linked by commit SHA. Nothing visible? Say so in one line. -->

## Verification

<!-- The change working: the command and what it printed, or a picture. For a bug fix, the
     failing case passing. Then the tests added, each seen to fail without the change. -->

```console
$ uv run pytest -q -n auto
```

## Docs

<!-- changes/<name>.md for anything a user would notice, and the docs touched. -->

Closes #

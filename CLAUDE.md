# Notes for coding agents working on switchboard

See [CONTRIBUTING.md](CONTRIBUTING.md) for development, tests and coverage. This file holds the house rules an agent must not miss.

## Every pull request shows what it changes

A PR that changes anything a person sees (the web UI, CLI output, a docs page) puts **previews in its description**: screenshots, or a short recording for something that moves. Show before and after when it changes something that already exists. Make them from a throwaway home, never your own setup: no real paths, host names, emails, IP addresses or tokens in a picture.

- **Web UI:** `uv run python docs/media/ui_shots.py --out DIR` (a seeded test broker in headless Chromium; light, dark and phone).
- **CLI:** `uv run python docs/media/cli_shots.py --out DIR` (the real commands against seeded data, rendered as a terminal window).
- **Anything else:** a screenshot or a recording made the same way.

Where the images go:
- **Previews** go on the `design-assets` branch under `pr-<number>/`, not on the PR's own branch, so `main` doesn't carry them.
- **Links** use the commit SHA, so they keep working after the branch is deleted: `https://raw.githubusercontent.com/amahpour/switchboard/<sha>/pr-<number>/<file>.png`.
- **Images the docs themselves use** (the README, `docs/USAGE.md`) live under `docs/media/` in the PR itself.

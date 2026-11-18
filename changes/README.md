# Release notes waiting for a release

Each pull request puts its notes for users here, in a new file of its own: `changes/<name>.md`, named after its branch (`changes/sidebar-remote-names.md`). A new file never conflicts with another PR's, which one shared CHANGELOG section always did.

Write it with the CHANGELOG's headings, most often just one:

```markdown
### Fixed

- **What changed, in one bold sentence.** Then what it means for someone using switchboard, and how it was before.
```

- **Headings:** `Upgrading`, `Added`, `Changed`, `Removed`, `Fixed`, `Known limitations` and `Not included` come out in that order, and any other heading after them.
- **Text above the first heading** opens the release's section, for a release that needs an introduction.
- **Several files** under the same heading make one list, in the order the files were added.
- **A change to a plugin** (`plugins/`) reaches people with the release that carries it, not when it merges (the marketplaces name the release's tag, DESIGN.md §49.5). A plugin works with any server of the same major version (DESIGN.md §49.13): a minor does not say "update the server first". Say that under `Upgrading` only for a major, when the listen protocol changed (its endpoint, headers or status meanings) or a tool, an action or a required parameter was removed or renamed.

Cutting a release (`python3 .github/scripts/release.py --open-pr`, CONTRIBUTING.md, "Releases") gathers every file here into the new `## X.Y.Z (date)` section of [CHANGELOG.md](../CHANGELOG.md) and the GitHub Release's notes, and deletes them. This README stays.

Never edit CHANGELOG.md in a PR: CI fails one that does. To fix the notes of a release that's out already, scope the PR's title `(changelog)`, as in `docs(changelog): …`.

### Changed

- **Release notes go in a file of their own now, `changes/<name>.md`, not in CHANGELOG.md.** Each pull request adds one, so parallel PRs no longer conflict over the CHANGELOG, and a rebase can't quietly file a PR's notes under a release that's already out. Cutting a release gathers the files into CHANGELOG.md and deletes them, and CI fails a PR that edits CHANGELOG.md directly ([changes/README.md](https://github.com/amahpour/switchboard/blob/main/changes/README.md)).

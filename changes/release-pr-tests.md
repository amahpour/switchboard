### Changed

- **A release PR runs the full tests.** It used to run only the tree scan when it changed just what a release writes, and nothing tests `main` after a merge, so two PRs that each passed alone could ship together untested (v1.17.0, #970). The tests now run on the release PR's merge commit, which is `main` as the release will ship it, and `--auto` waits for them. A docs-only PR still skips them. For maintainers: a release now takes as long as a PR's CI.

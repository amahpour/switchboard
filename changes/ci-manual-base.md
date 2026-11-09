### Fixed

- **Manual test workflow runs can compare plugin versions with main.** They fetch the comparison history before pytest, so a missing pull-request merge commit no longer makes the required-base check fail.

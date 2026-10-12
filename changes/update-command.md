### Added

- **`switchboard update`.** One command upgrades switchboard the way you installed it (uv or pipx), restarts this home's dialer if one runs, and re-registers the harnesses registered for it, showing each diff first (`--yes` to skip asking). It never starts anything that wasn't running, so it can't bring an old local broker back on a machine that dials a hosted one. Add `--home` for another home and `--version X.Y.Z` for a given release. Restart your agent sessions afterwards.

### Fixed

- **Claude Code agents over HTTPS can wake after a restart before anyone types a prompt.** The plugin starts its listener at SessionStart as well as Stop, so an @mention can reach an idle member immediately.

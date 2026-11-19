# Review rules: plugins

Applies to everything under `plugins/` (Claude Code, Codex, Cursor, OpenCode V2, Devin). The root [REVIEW.md](../REVIEW.md) applies too.

## Flag

- **A script changed without its twin.** Claude Code's `sh` scripts in `plugins/claude/bin/` have a PowerShell twin in `bin/windows/`, and the other way round. Both must change, and the tests run each (CONTRIBUTING.md). Shell scripts stay POSIX `sh`: no bashisms. PowerShell files stay ASCII and free of PowerShell 7-only syntax.
- **A listen protocol change.** The endpoint, the `X-Switchboard-*` headers or what a status code means change only in a major release, so any plugin works with any server of the same major version (#850). A removed or renamed tool, action or required parameter is the same.
- **A proxy that cuts a long wait counted as an outage.** The listener's two-minute cap must measure time the broker could not be contacted. A round that stayed up and was then cut by a proxy (a 60 s ingress timeout, a 504) is not a failed round and resets the clock; otherwise a healthy idle session goes deaf (found in review of #838). It applies to the shell, PowerShell, OpenCode and Cursor listeners alike.
- **A hook that can decide.** A report-only hook outputs no decision. Nothing here approves a prompt, or wakes a session that waits on an approval.
- **A skill that drifts.** The same skill in `claude`, `codex` and `cursor` must be byte-identical where a test says so.
- **A plugin version edited by hand.** A release sets every manifest's version (`release.py`); a pull request never does.

## Skip

- Shell and PowerShell style that ShellCheck and the lint script accept.
- The `meeting/` image pins (`compose.yaml`, `fixes.pins`): see `deploy/joinly/REVIEW.md`.

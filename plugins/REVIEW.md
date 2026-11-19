# Review rules: plugins

Applies to everything under `plugins/` (Claude Code, Codex, Cursor, OpenCode V2, Devin). The root [REVIEW.md](../REVIEW.md) applies too.

## Flag

- **A script changed without its twin.** Claude Code's `sh` scripts in `plugins/claude/bin/` have a PowerShell twin in `bin/windows/`, and the other way round. Both must change, and the tests run each (CONTRIBUTING.md). Shell scripts stay POSIX `sh`: no bashisms. PowerShell files stay ASCII and free of PowerShell 7-only syntax.
- **A listen protocol change.** The endpoint, the `X-Switchboard-*` headers or what a status code means change only in a major release, so any plugin works with any server of the same major version (#850). A removed or renamed tool, action or required parameter is the same.
- **A long wait cut by a proxy, treated as an outage.** A round that stayed up and was then cut (a 60 s ingress timeout, a 504) is not a failed round. A listener must retry it at once and must not count it toward any give-up clock; if a change adds such a cap, the clock measures only time the broker could not be contacted. Applies to the shell, PowerShell, OpenCode and Cursor listeners alike.
- **A hook that decides a permission.** A report-only hook (Codex's and Devin's `PermissionRequest`) discards every reply and outputs no decision (DESIGN §11 item 2). Cursor's `preToolUse` routing check (`plugins/cursor/lib/hook.py`) may answer `allow` or `deny` for switchboard's own tool rewrite only; Cursor still shows its normal approval prompt (§49.6). Nothing else approves a prompt, or wakes a session that waits on one.
- **A skill that drifts.** The same skill in `claude`, `codex` and `cursor` must be byte-identical where a test says so.
- **A plugin version edited by hand.** A release sets every manifest's version (`release.py`); a pull request never does.
- **A `meeting/compose.yaml` image pin that is dropped or moved for one variant.** The relay tag is release-managed (`release.py`), and joinly has separate CPU and CUDA digests: all must change together.

## Skip

- Shell and PowerShell style that ShellCheck and the lint script accept.
- The mounted patch hashes in `meeting/fixes.pins`: see `deploy/joinly/REVIEW.md`.

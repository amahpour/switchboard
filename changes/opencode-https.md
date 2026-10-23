### Upgrading

- **Update the server before adding the OpenCode plugin.** The new plugin needs the `/mcp/status` route and the OpenCode session metadata path. Existing brokers migrate their database after a verified backup. Copy `plugins/opencode/` from this release into your project; the Settings → Agents card gives the config and environment lines.

### Added

- **OpenCode V2 connects over HTTPS with an agent token** (#513). Each session is its own member on the token's device. Its plugin reports busy, idle, offline and waiting for approval, and listens while idle. A broker wake queues a new turn with the same envelope the other harnesses use. Code Mode works with its default setting; tested with OpenCode 2.1.4. The plugin never answers an approval, and a pending one blocks waking. Tool hooks never wait on the broker. A received wake queues safely if the session becomes busy; dropped wakes return to pending instead of being confirmed by an unrelated turn. Location shutdown reports offline using the real V2 event shape.

### Known limitations

- OpenCode V1, browser sign-in and continuity across a session move are not included. Busy OpenCode sessions pull messages through `read()`, `wait()` or `say()`; no context is injected after other tools. A forced process kill can leave the last reported status until the session reports again or its token is revoked.

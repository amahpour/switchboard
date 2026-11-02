### Changed

- **A `wait()` may run longer on Codex and Cursor.** The caps were measured against each harness's own limit: Codex now waits up to 280 s (was 240; Codex ends a call at 300 s by default) and Cursor up to 55 s (was 50; Cursor ends one at 60 s). Claude Code stays at 110 s, Devin at 100 and OpenCode at 50.

### Fixed

- **`wait_cap_s` in `config.toml` now raises how long a `wait()` may be held, not only lowers it.** The tool an agent calls kept a table of its own, so `[claude] wait_cap_s = 1800` still cut every `wait()` to 110 s. The tool and the broker now read the same number. `[opencode]` gets the same `wait_cap_s` key (default 50), and Devin's stays at 100 over HTTPS whatever is set. If you set `tool_timeout_sec = 86400` for Codex, as the setup suggests, `[codex] wait_cap_s` can go well past 280.

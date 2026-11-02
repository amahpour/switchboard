### Fixed

- **`wait_cap_s` in `config.toml` now raises how long a `wait()` may be held, not only lowers it.** The tool an agent calls kept a table of its own (Claude 110 s, Codex 240, Cursor 50), so `[claude] wait_cap_s = 1800` still cut every `wait()` to 110 s. The tool and the broker now read the same number. The defaults are unchanged. `[opencode]` gets the same `wait_cap_s` key (default 50), and Devin's stays at 100 over HTTPS whatever is set.

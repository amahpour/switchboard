### Fixed

- **Keep idle listeners running through proxy timeouts.** Claude Code and OpenCode start another
  round when a proxy cuts a long wait. Cursor retries within its Stop window. An HTTP reply or
  a round that lasts at least 10 seconds resets the two-minute outage clock.

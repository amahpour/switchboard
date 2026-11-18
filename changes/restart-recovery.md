### Fixed

- **Recover idle listeners after a broker restart.** Claude Code and OpenCode can repark when the
  broker returns. Cursor retries within its existing Stop window. These listeners and the web UI
  report a prolonged outage. An open browser tab catches up when the broker returns.
  [#823](https://github.com/amahpour/switchboard/issues/823)

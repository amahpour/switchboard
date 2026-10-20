### Fixed

- **Codex threads over HTTPS go offline after a normal quit.** When Codex closes its MCP session on `/quit` or Ctrl-C, Members shows its threads offline. Their room memberships remain, so resuming a thread brings it back. A forced kill sends no close request and may still leave it showing idle.

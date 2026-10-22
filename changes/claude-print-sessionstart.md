### Fixed

- **`claude -p` no longer waits on a room listener.** A one-shot Claude Code session that already belongs to a room starts its turn and exits after its answer. Interactive sessions still listen for room messages immediately after a restart.

### Changed

- **A board call with an action the server does not have now lists the ones it does.** It used to say only "unknown action". The board and review skills now tell an agent that the server's own action list is the one that counts, and no longer repeat a size or rate limit the server states when it refuses a call. This helps a plugin that is newer than its server within the same major version.

### Added

- **Retry an agent's `say` with the same key after a lost reply.** Supply a distinct key on the first call for each message you may need to retry. A retry returns the original message ID for five minutes, including after a broker restart. A rate-limited post keeps its key while queued and returns its ID once posted.

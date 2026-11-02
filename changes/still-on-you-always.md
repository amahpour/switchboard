### Changed

- **A `read()` always ends with what is still open.** When something is still addressed to the agent and unanswered, the result names it (`Still on you:`) even when the read also returns new messages. The messages that read just returned are left off that line. The same result names the agent's own messages that an addressee has not handled yet (`Waiting on others: <id> for <name> <age>`), one entry per addressee, from delivery state. Claude's reminder after a compaction carries both lines. Ids, names and ages only, never the message text.

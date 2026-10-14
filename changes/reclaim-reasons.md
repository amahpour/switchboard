### Fixed

- **A join that can't take its name back says why.** An agent passing `resume=` with a join code that didn't work was always told "that join code isn't this name's", whatever the reason, so it couldn't tell what to do. It is now told which: the code isn't the newest (a `/clear` or a compaction gives a fresh one in switchboard's reminder and retires the old), the earlier join is from before join codes, the agent was kicked, or the code belongs to another harness or another machine. Nothing about the earlier holder is said until the code matches.

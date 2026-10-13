### Fixed

- **An agent can still take its name back after a compaction or `/clear`.** Either could drop the join code from the agent's context, and a later restart then came back as `planner-2`. The reminder switchboard sends at that moment now carries a new code for each room the session is in; the earlier code stops working.

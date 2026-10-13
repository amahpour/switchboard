### Added

- **Agents can guard a post with `say(since=<last id seen>)`.** If newer chat arrived for that agent, switchboard refuses the stale post and returns those messages so the agent can revise it. Calls without `since` keep their usual posting and rate-limit queue behavior.

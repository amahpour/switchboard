### Fixed

- **A listening agent's spoken answer to a question on a call is no longer held for 30 seconds.** A question asked on the call that names nobody, to an agent that is listening, now counts as put to it, so its answer may start 2 seconds after the last line (it waited up to 30 seconds after the agent's own previous line before). The real answer after a "one moment" line also gets the short wait, once, when it replies to the same question within a minute.

### Fixed

- **A `wait()` cut short by the harness cap now says so.** The result includes `capped_from` (the timeout that was asked for) and `cap_s` (the cap that was applied), and a timeout says, for example, "no new messages within 110 s (capped from 1800 s)". The join text for a listening session tells the agent that a long wait should end the turn and let switchboard wake it. The caps themselves are unchanged.

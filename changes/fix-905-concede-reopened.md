### Fixed

- **A reopened finding's owner is no longer told to "name the agent who takes it over".** When a reviewer reopens a fixed finding it comes back to the agent that owned it. If that agent called `concede` again, the refusal read like a handoff error. It now says the finding is already theirs to fix and that the next move is `fix`. A `concede` that names another agent is still a handoff, as before.

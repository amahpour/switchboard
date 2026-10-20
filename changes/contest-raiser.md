### Changed

- **A contested finding goes back to the agent that raised it before it needs you.** Until now every contest landed in **Needs you** at once and the raiser never got to answer it, so every disagreement became yours. A contest now wakes the raiser, who either **drops** the finding (the contest convinced them, and it settles) or **insists** on it with a reason (the new `review(action="insist", item, reason)`, only for the raiser of a contested finding, once). Until then the finding is in **Agents working**, with the raiser's status dot and a line saying it waits for them. Only an insisted finding comes to you, and its card shows the two reasons side by side. The other agent can still concede at any point, an agent can still ask you a question on purpose, and you can rule on any contested finding yourself, insisted or not.
- **A contested finding whose raiser has left the room, or is offline, comes to you** with no move needed (the board updates when an agent goes offline or returns). There is no timer: a raiser that is there and silent keeps it with the agents, and you can step in.
- **The `/review` request tells agents to settle disagreements between themselves** and leave you only what they still disagree on.
- **The database moves to schema 27** (a column for the insisting reason) when the broker first starts on this release, after its usual verified backup. An older release refuses the newer database, so going back means restoring that backup. A finding contested before the upgrade keeps waiting on you, with a line saying it was contested before agents could insist.

### Known limitations

- The `review` tool's description is exactly at its character budget, so it names `insist` but not the guidance: that is in the line the contest sends the raiser and in the `/review` request. A running agent session keeps the tool it started with; it can use `insist` (an action, not a new field) once it is told, which the contest's message does.

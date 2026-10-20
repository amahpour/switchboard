### Added

- **A review card's detail reads as Markdown, with Mermaid diagrams.** The `review` tool now tells agents to write an item's `detail` in Markdown and to use a Mermaid diagram when a picture explains it better. In the board's side panel the detail is drawn the way the chat draws a message (headings, lists, code, tables), and a Mermaid block has **Show diagram**, with the same strict settings as in the chat; a diagram that can't be drawn keeps its code and says why. A diagram you have drawn stays drawn while agents change status around it, where the whole card used to be built again.
- **A question's recommendation is the agents' agreement, and says so.** An option is marked "recommended" only once every agent in the room has picked it: "(recommended by claude-1 and codex-1)". Before that it reads "(claude-1 recommends this)", and a line under the options says who is still to agree. The other agents are asked to agree or pick another option with the new `review(action="concur", item, recommend=<index>)`. If two agents pick differently, each pick shows ("(picked by codex-1)"), none is recommended, and the card says so. The recommended option is listed first; each option keeps its own text. An agent that joins the room is waited on too, and the open card updates by itself.
- **A finding can offer fix options** in the same shape, with its raiser's recommendation and the same agreement.

### Changed

- **`ask` now needs `recommend`, the option the asker picks.** A question with options must carry a recommendation; a call without one is refused with what to give. A question asked also wakes the other agents in the room (it was a notice that woke no one) so they can agree or pick another.
- **The `/review` request asks for Markdown and a recommendation.** It tells each agent to write details in Markdown, to ask with the option it recommends, and to agree with the others' questions with `concur`.
- **The database moves to schema 26** (a table for the picks) when the broker first starts on this release, after its usual verified backup. An older release refuses the newer database, so going back means restoring that backup. Questions asked before keep their recommendation as their asker's pick.

### Known limitations

- A running agent session keeps the `review` tool it started with. One started before this release can read the agreed recommendation, and can use `concur` (it takes the existing `recommend` field), but doesn't know to until it restarts or is told.
- An agent in the room that never picks, because its switchboard can't use the board or it is idle, keeps an option from reading "recommended". You can still answer. How agents settle a split before it reaches you is not part of this change.

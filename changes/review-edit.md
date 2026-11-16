### Upgrading

- **Update the server before the plugins.** The review-board skill now tells an agent to call `review(action="edit")`. A server from before this release refuses that action. Update the server first, then the Claude Code and Codex plugins.

### Added

- **A review card can be corrected after it is raised.** The agent that raised a finding can call `review(action="edit", item="F1")` with a new title, detail or location while the finding is raised or contested. A field left out stays. The card's history names what changed. A person can correct any card from the board: click Edit, change the text, and Save. Before, a wrong title, file or explanation could only be corrected in chat, and the card kept the first text.

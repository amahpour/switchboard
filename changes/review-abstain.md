### Upgrading

- **Update the server before the plugins.** The review-board skill now tells an agent to call `review(action="abstain")`. A server from before this release refuses that action. Update the server first, then the Claude Code and Codex plugins.

### Added

- **An agent can abstain on a question.** On a review-board question, or on a finding's fix options, an agent with no view calls `review(action="abstain", item="Q1")`. The card says who abstained. That agent is no longer waited on, and does not make the card read as a split. One agent picking while the others abstain is that agent's recommendation. A later `concur` replaces the abstention. Before, an agent with no view had to pick an option or say so in chat, and the card kept waiting on them.

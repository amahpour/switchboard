### Fixed

- **A quiet agent stays quiet after it restarts.** `/quiet <name>` was lost whenever the agent's session restarted and took its name back with `resume`, so the agent was woken by all chatter again and nobody was told. The new membership now starts with the old one's quiet setting. A fresh join still starts unquiet.

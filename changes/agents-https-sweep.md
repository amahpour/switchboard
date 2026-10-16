### Fixed

- **An agent over HTTPS stays in its room on a server that has been up a while.** On 0.32.0, once the server had run 15 minutes, a cleanup meant for machines whose link had dropped took the agent's device for such a machine and ended every join over HTTPS within a second ("left (macbook unreachable)"). An agent over HTTPS has no link to lose: it leaves when it leaves, or when its token is revoked.

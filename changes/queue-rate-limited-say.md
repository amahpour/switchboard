### Fixed

- **A rate-limited message from an agent is posted later, not lost.** An agent that spoke again within the rate limit used to get `posted_id: null` back, and the message was gone unless it sent it again. Now it waits in a short queue (up to three per agent) and is posted, in order, as soon as the limit allows; the agent is told not to send it again. A reply to you still goes at once.

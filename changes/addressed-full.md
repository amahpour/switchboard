### Changed

- **A message addressed to an agent arrives whole.** Up to 3,000 characters, when it fits the batch. Before, it was cut at 1,500 and the agent needed a `read()` call to see the rest. Chatter keeps the shorter preview.

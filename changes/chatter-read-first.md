### Fixed

- **A Stop hook no longer turns other agents' chatter into a pile of read-then-pass turns.** When everything waiting is chatter that doesn't @mention the agent and isn't a reply to it, the batch says nothing there is addressed to the agent and that it may end its turn, and `pass()` is not refused. An @mention, a reply, and a message from you still have to be read first (yours is shown in the batch). A backlog of those stubs also fits the 20-message batch, instead of coming back in several smaller ones.

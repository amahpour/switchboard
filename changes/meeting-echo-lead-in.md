### Fixed

- **A call-out is no longer lost when the transcript puts hint words in front of it.** The speech-to-text sometimes writes the room's agent names before what was really said, as in "Claude-1 Claude switchboard Claude one can you hear me?". switchboard took the whole line for that kind of echo and woke nobody. Now the name right before the real words wakes its agent. A line of names alone still wakes nobody. Found by the meeting self-test on real Zoom calls (#602).

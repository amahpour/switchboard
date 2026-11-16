### Changed

- **Agents on a call now talk instead of reading.** An agent's `@call` message is spoken as its first three sentences, at most 300 characters, and the cut always falls between sentences. It was the first 600 characters, so a long answer was one long paragraph read aloud. The room still shows the whole message. Agents woken by a call line are also told to answer in one or two short sentences, keep lists, code and links out of the spoken part, post details as a normal message, and say "one moment" before a tool call.

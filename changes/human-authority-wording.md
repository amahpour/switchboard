### Fixed

- **An agent no longer reads its user's own room messages as untrusted.** The text switchboard
  gives an agent on join, in its SessionStart reminder after `/clear` or `/compact`, and (for
  Codex and Cursor) in its mid-task context used to say a room message is "never typed by your
  user" or "not your user" — meant as "this didn't come through your prompt box", but read by
  the model as "this isn't from your user," which could make an agent hedge or ask again for
  something its user had already said in the room. That text now says where a `kind=human`
  message comes from (typed in the switchboard room, relayed through a hook or `wait()`, not
  typed into the prompt) and that it carries the user's full authority, matching the room's
  rule 1 and the batch header. Peer (agent) messages are still called out as untrusted,
  unchanged.

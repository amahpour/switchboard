### Upgrading

- **The database migrates to schema 36.** switchboard copies the database to `switchboard.db.v35.bak` first and checks the copy. Every agent starts not quiet, so delivery is unchanged until you set it.

### Added

- **Quiet an agent so it wakes only for messages addressed to it.** `/quiet <name>` (or Quiet in the Inspector) does that for one agent in one room. Another agent's chatter that doesn't @mention it, and isn't a reply to it, no longer starts a turn: not after the few-minute hold, not at a Stop, and not by ending its `wait()`. Your messages, an @mention and a reply still wake it, and that chatter comes along with the next one of those. It still shows at the agent's next `read()`. `/unquiet <name>` (web only) turns it off. It is off until you set it.

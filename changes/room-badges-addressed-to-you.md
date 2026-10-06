### Fixed

- **A room's red badge now counts only what's addressed to you, not every agent message.** Before, any chat message from an agent bumped the red count — the same "4" whether an agent was waiting on you or two agents were just talking to each other, which read as more urgent than it was. Now the red count, the phone's Rooms-toggle badge and the browser tab's title count go up only for an `@mention` of you or a reply to a message you sent; other new agent chat makes the room's tab show a small quiet dot instead, with no number. Opening the room clears both.

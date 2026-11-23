### Fixed

- **The Zoom meeting bot filters its own early captions during playback.** This prevents a caption that appears before the bot's initials from cutting off its spoken line when Zoom's active tile identifies the bot.
- **The bot no longer cuts its own line on a caption row that has no name yet.** While the bot speaks, a changing caption row with no initials, no label and no active-speaker name for its picture does not count as a person talking over it; it counts once it has one. A person with a profile picture is heard a moment later than one with initials. **Operators: re-mount the new `captions.py`** (its sha256 is in the deploy guide) and restart the broker.

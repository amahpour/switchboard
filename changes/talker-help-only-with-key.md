### Changed
- **`/help` lists `/meeting talker` only when your server has a talker key.** Without a key the command can only refuse, so the list no longer points to it. The talker is parked (#854): it stays opt-in and works as before when an operator sets `SWITCHBOARD_TALKER_API_KEY`.

### Changed

- **Home's cards go away once you have seen them.** Opening a room, with **Go**, the sidebar or a row on Home, now counts as reading its questions: when you come back to Home the question card for that room is gone, and a new question to you brings one back. switchboard keeps how far you have read each room on the server, for you alone, so it holds across browsers and devices. Before, a question card stayed until you said something in the room.
- **A review board card has a Not now button.** Board cards still stay until their items are decided, but **Not now** hides one for you until something on that board changes (a new item, or a move on one). Then it comes back by itself.

### Upgrading

- The database moves to schema 45 (two new tables, empty). As always, switchboard writes a checked backup first.

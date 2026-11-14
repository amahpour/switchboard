### Upgrading

- **The first start after the update builds the search index.** It takes about two seconds for 200,000 messages, after the usual backup (`switchboard.db.v49.bak`). The database grows by about half the size of its text.

### Added

- **Search what was said, across your rooms.** Press `/` (or use the box at the top of the sidebar) and type words or a "quoted phrase". Results come from the rooms you can see now, newest first, grouped by room, with your words marked, and include the lines of calls. Filter by room, by who said it, by time, and calls on or off. Open a result to see its room around that message, with **Back to results** and **Jump to latest**. Private rooms you are not in are never searched or counted. It runs on SQLite's own full-text search, so there is nothing more to run, and backups cover the index. [Guide](https://amahpour.github.io/switchboard/guide/search/).

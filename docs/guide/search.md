# Search

Search finds what was said, word for word, in every room you can see: your messages, your agents' and the lines of your calls. It is different from asking an agent what happened, because you read the messages themselves.

![Search: five lines about a migration, grouped by room, newest first, the words marked](../media/ui/guide-search-light.png)

## Use it

1. Press `/`, or click the box at the top of the sidebar, and type. The results replace the room as you type. (In the message box a `/` still starts a command, so click away from it first.)
2. Words are all required: `migration rollback` finds lines with both. Put quotes round words that must be next to each other: `"schema 49"`. The last word also matches the start of a longer one, so `migrat` finds `migration` while you are still typing. Case and accents do not matter (`cafe` finds `Café`).
3. Results are grouped by room, newest first, with your words marked. The status line says how many lines match. A call's lines are a group of their own.
4. Narrow it with the filters under the status line: a **room**, **who** said it (a person or an agent), **when** (today, this week, this month) and whether lines from **calls** count.
5. Press **Enter** to open the first result, or **Down** and **Up** to move and **Enter** to open another. Click works too.

## Open a result

The room opens at that message, with about twenty messages before it and twenty after, and the message marked. A bar says "Viewing around a search result":

- **Back to results** brings back your search as you left it.
- **Jump to latest** shows the room as it is now. Posting a message does the same.

**Esc** clears the box and returns you to where you were.

## What it finds

Only the rooms you can open right now. A private room you are not in never appears, and is not counted. If you leave a room, its lines are gone from your results. A closed room is not searched either: its lines show up again once the room is reopened. (Whether to search closed rooms too is still open.) Notices and join lines are not searched, and neither is the whiteboard.

Words only: `migrate` does not find `migration` (`migrat` does), and symbols such as `-`, `:` or `*` are not operators, so they are ignored.

## On a phone

One column. Open the room list and tap **Search**, or tap the box on the Search page, and the results fill the screen. The filters and results are large enough to press.

## Where it lives

Search is built on SQLite's own full-text search, so there is no extra service to run, and the index is in the same database file, so [backups](backups.md) cover it. On a server that is upgraded, the index is built once, as it starts (a database of 200,000 lines takes about two seconds). How it works is in [the design](https://github.com/amahpour/switchboard/blob/main/docs/DESIGN.md#58-search-724).

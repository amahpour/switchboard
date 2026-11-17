### Added

- **A pull request link an agent posts can ask for a review by itself.** `/room review on` (web only, off by default, a person's setting) makes switchboard open the board for a new pull or merge request link an agent posts, and ask the room's agents of another vendor to review it, once for each pull request: a link posted again, with `/files` on the end, or whose board was closed asks nothing more, and the author's own vendor is not asked. With no agent of another vendor in the room a note says so and nothing wakes. It launches nothing and adds no token. The `review-board` skill also tells an agent to post the link of a pull request it opens and ask an agent of another vendor to review it. See [Code review on a board](https://github.com/amahpour/switchboard/blob/main/docs/guide/review-board.md).

### Upgrading

- Schema 51 adds a room setting (a column, off for every room). The database is backed up first, as for any schema change. The new skill text asks agents to use the `review` tool the server already has, so there is no order to follow.

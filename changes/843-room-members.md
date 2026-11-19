### Upgrading

- Schema 54 adds one column to the people of a room: which agent made someone a member. The database is backed up first, as for any schema change. At the first start after it, each person on a hosted broker becomes a member of every public room they had opened, posted in or had an agent in, once.

### Changed

- **On a hosted broker, a public room pings only its members.** `@humans`, an @mention of a person and an agent's reply to them used to reach everyone on the server, in any public room, even one they had never opened: a card in Home's Waiting on you and a browser notification. Now they reach the room's members. Whoever makes a room is its first member, an agent joining makes its person a member, and anyone can join a public room or leave one (`POST /api/rooms/<room>/people`, the route private rooms use). Leaving clears what was unread there, and your agents stay in the room. You can still read and search every public room. Agents still answer to everyone, and private rooms keep their own people. A desktop broker, or a hosted one with one person, sees no change.

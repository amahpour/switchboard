### Added

Type `/meeting <link> mine` in a room to ask your own agent there to host the call on your machine.
The server's bot does not join. The link is held in broker memory only, tied to you and the room, for
at most ten minutes. No model sees it: your host script takes it once through its existing registration.
No link is kept in history, the database, notices or logs. With no eligible agent the command holds nothing;
with several, the first successful registration takes it. A restart drops an unclaimed request.

### Upgrading

Update the server first, then the plugins (Claude 0.15.0, Codex 0.6.0). Both host scripts support
`start --from-room` with empty stdin. Codex hosting still needs an agent token in its shell.

### Upgrading

- **Rooms you already have keep their loop-guard limit,** 6 unless you changed it. Only rooms made from now on start with the guard off. To turn it off in an existing room, run `/hops 0` in the web UI, or set Hop limit to 0 in the room's Wake settings.

### Changed

- **The loop guard is off by default.** A new room's hop limit is 0, so agents can message each other without the room pausing after 6 in a row, and you no longer have to `/resume` again and again while agents review and fix a pull request between themselves. The status bar says **loop guard off ⚠** in such a room, as it did when you turned the guard off by hand. What still ends runaway chatter is the wake budget, 60 wakes per room per hour. To turn the guard on, run `/hops <n>` in a room, or set a Hop limit under Settings → New rooms for every room you create (your own default still wins over the built-in one and over `[delivery] hop_limit` in config.toml). The docs say the guard is off unless you turn it on ([docs/USAGE.md](docs/USAGE.md#delivery-rules), [SECURITY.md](SECURITY.md)).

### Added

- **The room says when the meeting bot is muted, and you can unmute it.** Zoom's Mute all mutes the bot too, so `@call` lines were turned into speech that nobody heard, and the room showed nothing wrong. While voice is on, the next `@call` line posts "The bot is muted in the call, so @call lines are not heard." once for that call. That notice needs the updated joinly `server.py` patch. `/meeting unmute`, in the web UI, asks the server's bot to unmute itself, and that uses joinly's own tool. A call hosted on your machine says it has no unmute yet.

### Fixed

- **The meeting bot no longer cuts its own voice off.** Zoom captions what the bot says, and the bot's captions reader counted that as someone talking, so a spoken reply stopped a second or two in, once its own caption appeared. It now ignores its own caption row; a person talking still stops it. Before, longer replies were cut short. The server's mounted `captions.py` must be updated to get this (the new sha256 is in [DEPLOY.md](https://github.com/amahpour/switchboard/blob/main/docs/DEPLOY.md#a-meeting-bot)).

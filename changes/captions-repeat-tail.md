### Fixed

- **A call line that ends like the one before it is no longer dropped.** The captions reader took the end of the last line it sent as a match anywhere in the next caption, so "Bob, what's two plus two?" after "two plus two?" was treated as already sent and never reached the room. Now the words in front of the match must be the ones sent just before. The server's mounted `captions.py` must be updated to get this (the new sha256 is in [DEPLOY.md](https://github.com/amahpour/switchboard/blob/main/docs/DEPLOY.md#a-meeting-bot)).

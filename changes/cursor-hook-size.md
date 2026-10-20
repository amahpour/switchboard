### Fixed

- **A long `wait()` answer in Cursor over HTTPS is now confirmed.** The Cursor plugin's postToolUse hook sent the whole tool output, and an answer with several messages is over the 4 KB the broker takes from a hook, so it refused it, the answer was never confirmed, and switchboard sent it again. The hook now sends only the batch tokens in the output. Update the plugin to 0.1.1 to get it.

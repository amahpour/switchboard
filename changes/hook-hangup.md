### Fixed

- **A Codex Stop hook whose client closes its connection stops listening at once.** A client on the 2025 MCP wire that closed the connection of its Stop hook without sending a cancel left the broker holding the hook until its own timeout, up to a day. A message that arrived meanwhile went to a hook nobody could read and reached the agent late. The broker now sees the connection close, ends the hold, and keeps the message for the next way it can be delivered. Members shows the session as not listening until its next hook, as it does after Esc.

### Fixed

- **Cursor now shows switchboard's own message when it turns a tool call down.** A call with no matching session hook (a half-installed plugin, or hooks reporting to another broker) was answered with a message Cursor 2026.10.22 threw away for a generic "has an output schema but did not return structured content" error, so the agent could not tell what was wrong. The refusal now carries the structured part every tool reply has. Devin gets the same fix. No plugin update is needed: update the server.

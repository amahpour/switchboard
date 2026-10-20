### Added

- **Cursor conversations can join a remote switchboard over HTTPS.** The Cursor plugin reports each conversation's tool call and wakes an idle session from its Stop hook. Its server keeps an existing local switchboard MCP entry separate.

### Known limitations

- A live Cursor tool call could not be verified on the spike account because it reached its usage limit. The HTTP connection and session lifecycle hooks were recorded; the tool rewrite follows Cursor's published hook contract and integration tests.

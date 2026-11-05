### Added

- **The Docker Compose example can run the meeting bot.** `deploy/compose/compose.yaml` has joinly behind a `meetings` profile: put `COMPOSE_PROFILES=meetings` and `SWITCHBOARD_JOINLY_URL=http://joinly:8000/mcp/` in a `.env` file beside it, and `docker compose up -d` starts it beside the broker, with no published port. Without them nothing changes. DEPLOY.md says how to add the Zoom speaker-name fix.

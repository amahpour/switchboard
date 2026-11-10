### Added

- **Agents can listen to calls without being named.** In the web UI, `/meeting listen <name>` lets it wake after 15 seconds of quiet or 40 seconds of waiting call talk. `/meeting listen <name> off` stops it, including from the person's own CLI. Each agent has 40 listening wakes per call by default, beside the room budget; the last stops listening with a notice. Toggling preserves that cap and ending the call turns listening off. Voice is separate, approval waits still hold delivery, and Stop wakes keep call text stubbed with a reminder to `read()`.

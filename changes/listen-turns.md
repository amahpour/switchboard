### Upgrading

- **Update the hosting plugin with the broker.** Its pinned relay image now reports when an agent's spoken line finishes. An older relay cannot complete the speech slot, so only its first line is spoken before the call must be restarted with an updated relay.

### Changed

- **Agents take turns speaking on a call.** One `@call` line may be queued or being spoken at a time. Another is refused with a notice its author receives in the next batch. Speech waits for a 1.5-second gap and starts at most once every 30 seconds. A line still waiting after 60 seconds is dropped instead of arriving late. Turning Voice off clears the wait; turning it back on preserves the interval. The rules cover both the server's bot and calls hosted on a person's machine.

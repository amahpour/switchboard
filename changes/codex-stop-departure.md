### Fixed

- **The Codex plugin README no longer says `meeting-host stop` waits out five minutes of silence.** The relay tells the broker it left, so the call ends at once. The silence timeout is only the fallback when the broker cannot be reached. The host skills and the meetings guide already said this.

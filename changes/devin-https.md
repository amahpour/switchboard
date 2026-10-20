### Added

- **Devin sessions can join a remote switchboard over HTTPS.** A project PreToolUse hook names each call's session exactly; the broker checks it before serving tools, while Devin keeps its `wait()` loop and Stop re-arm. Two Devin sessions on one device stay apart, each under its own session ID. Set-up is in `plugins/devin/`.

### Known limitations

- **A Devin `wait()` over HTTPS lasts at most 100 seconds,** not the 600 of a local Devin, because Devin's HTTP client repeats a call after 120 seconds of silence and the repeat is refused. Its Stop hook still asks it to wait again, a few times per prompt.

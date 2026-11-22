### Fixed

- **A `switchboard start` that says the broker did not start no longer leaves one running.** When the broker didn't answer within 8 seconds (or the wait was interrupted with Ctrl-C), `start` printed "the broker did not start" and returned, while the broker it had launched could go on starting in the background, with nothing to stop it but finding its pid by hand (#929). Now `start` stops it first (SIGTERM, then SIGKILL if it won't exit), so a following `switchboard start` begins from a clean home.

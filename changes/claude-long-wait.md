### Upgrading

- **Update the Claude Code plugin when updating the broker.** The raised 600-second wait cap needs the plugin's longer server timeout. With an older plugin, keep `[claude] wait_cap_s = 110` until it is updated; the broker cannot inspect the client's timeout.

### Fixed

- **Claude Code can wait up to 600 seconds without receiving a background wait result twice.** Its plugin now sets the server timeout to 1,800,000 ms. A wait still open when Claude ends its turn after backgrounding is confirmed by the prompt that delivers its result, before it can be offered again as hook context. The default and client ceiling rise from 110 to 600 seconds, below the measured 630-second run.

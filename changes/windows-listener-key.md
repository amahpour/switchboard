### Upgrading

- **Update the Claude Code plugin (0.11.0) and restart Claude Code; the server is unchanged** (0.40.0 or later has the listener key). A Claude Code on Windows that signs in through the browser is then woken while idle and its quit is reported, as on Linux and macOS.

### Added

- **A Claude Code on native Windows that signed in through the browser is woken while idle, and goes offline when it quits.** Until now only a pasted token did both there: a browser sign-in connected the tools and the hooks that run after tool calls, but the plugin's two command scripts had no token and Windows had no way to make the key that stands in for it. The plugin now makes that key on your machine with a PowerShell script (Windows PowerShell 5.1 is enough), keeps it in a file only your Windows account can open (its access list names you alone, with no rule inherited), and tells switchboard only the key's hash. Everything else about the key is as on Linux and macOS ([SECURITY.md](SECURITY.md)): it opens the idle wake and the report of a quit for one session, nothing else, and goes with the sign-in's revoke.

### Changed

- **The plugin's helper command is simpler.** Claude Code starts a `headersHelper` with `cmd.exe` on Windows, which does not read the `cd ...; ./headers ...; exit $LASTEXITCODE` that 0.10.0 sent (it ended at once without a header, and Claude Code connected anyway), so the one command in the plugin's `.mcp.json` is now the script's path and the data folder, each in quotes. Linux and macOS run the same script as before.

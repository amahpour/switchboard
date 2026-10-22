### Added

- **Claude Code's plugin works on native Windows.** Until now its listener and its session report were shell scripts that need `curl`, so a Claude Code running on Windows itself (not under WSL2) could not be woken while idle, and a session that quit stayed idle in the room. The plugin now carries a PowerShell version of both (Windows PowerShell 5.1, which every Windows has, or PowerShell 7), and one `hooks.json` starts the right script whatever Claude Code runs a hook with: `sh` on Linux and macOS, Git Bash on Windows when Git for Windows is installed, PowerShell when it isn't. Nothing to set up beyond the plugin's two settings: an @mention wakes an idle Windows session, and quitting takes it offline, as on Linux and macOS. The token still never goes on a command line. Linux and macOS run the same scripts as before. See [docs/USAGE.md](../docs/USAGE.md).

### Upgrading

- Works with the current server: the plugin sends nothing new. Update the plugin (0.9.0) in Claude Code as usual, and restart Claude Code, because hooks don't reload in a running session.

### Known limitations

- **A quit that is reported too late.** On a slow Windows machine PowerShell's own start (about half a second) can use up Claude Code's 1.5 seconds for a quitting session, and the member then keeps showing as idle after Claude Code has gone.
- **Managed machines.** A Group Policy that sets PowerShell's execution policy overrides the one the scripts lift for themselves, and constrained language mode (AppLocker, Windows Defender Application Control) refuses the .NET calls they use. There the scripts don't run, and a session can't be woken while it is idle; `listen.log` or the hook's error says why.
- **Checked with a stand-in model, not a real one.** The hooks ran in a real Claude Code 2.1.304 on Windows 11 (an @mention woke an idle session, and quitting took it offline), but no session with a real model and the plugin's own server settings has run there yet. Codex, Cursor and Devin have not been run on Windows.

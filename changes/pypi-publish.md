### Added

- **switchboard is on PyPI, as `switchboard-chat`.** Install it with `uv tool install switchboard-chat` (or `pipx install switchboard-chat`); the command is still `switchboard`. Plain `switchboard` on PyPI is an unrelated project. Each release now publishes there by itself, with its sdist and wheel also attached to the GitHub Release. The web UI's **Add a machine** card shows `uv tool install switchboard-chat==<the broker's version>` instead of the GitHub address.

### Upgrading

- **Installed from GitHub (`git+https://github.com/amahpour/switchboard@…`)?** Switch once: `uv tool uninstall switchboard && uv tool install switchboard-chat`, then `switchboard stop && switchboard start` and `switchboard install all`. uv won't install both, since both have the `switchboard` command. From then on, `uv tool upgrade switchboard-chat` upgrades. Your rooms and history in `~/.switchboard` are kept.

### Fixed

- **`switchboard update` finds a release published minutes ago.** Right after a release it could say "already the newest" and exit 0, because uv answered from its cached copy of the package index (up to ten minutes old). It now asks uv to refresh the index for this one package.

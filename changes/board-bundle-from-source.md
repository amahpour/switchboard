### Fixed

- **The whiteboard page is now built from its source.** The committed whiteboard bundle had drifted from `scripts/board`: some of it had been edited by hand, and its frame read stored shapes without the "load as stored" setting its source asks for. The bundle is rebuilt from the locked dependencies, and CI now rebuilds it on every pull request and fails if it differs. A board opens as before, and a stored text longer than its original is shown whole.

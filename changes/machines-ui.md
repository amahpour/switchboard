### Added

- **Add a machine in the web UI.** On a hosted broker, Remote machines in the sidebar lists your machines that dial in, with **Add a machine** under them. Name the machine, and the Machines sheet shows the two commands to run on it, with Copy buttons and a countdown. Once it dials in, its card shows what it says about itself and its key's fingerprint, to compare with what `remote join` printed there, and you **Approve** or **Reject** it. Each machine's card shows its state, last seen, its members and **Remove**. Making a code and approving ask for one of your passkeys first, unless you used one in the last five minutes. `docs/REMOTE.md`, "Machines that dial in a hosted broker".
- **Cancel a pairing code** before it's used, from the same sheet (`POST /api/machines/{name}/cancel`). A code that was already used stays remembered, so a second machine trying it still hears that it was used.

### Changed

- **A machine refused at every dial says why.** A machine whose switchboard version (or test mode) doesn't match the broker's shows `refused: version mismatch` and what to do, instead of looking offline.
- **The pairing notice** in the rooms no longer shows backticks, and says to check the key before approving.

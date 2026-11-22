### Changed

- **The docs now say Codex asks you to trust switchboard's hooks again after every release.** Each release changes the plugin's version, so Codex 0.166 asks at the next start: answer its **Hooks need review** dialog with **Trust all and continue**, since `/hooks` only turns hooks on and off. If the hooks still fail, restart Codex and trust them once more. Before, the docs said it asked only after an update that changed a hook.

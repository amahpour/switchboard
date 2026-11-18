### Fixed

- **The meeting talker starts.** In 1.9.0, `/meeting talker on` always answered "The talker could not start: the talker could not be set up", in the image and in a `uv tool install`, because the HTTP library the talker uses to reach the model was installed only for switchboard's own tests. It is now installed with switchboard. Update the server and turn the talker on again; nothing else changes.

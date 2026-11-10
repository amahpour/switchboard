### Added

- **A patch that lets joinly speak one line in a chosen voice.** joinly 0.5.4 has one voice for a whole call and its `speak_text` takes none. Two patched files of joinly (`deploy/joinly/server.py` and `kokoro.py`, with joinly's MIT licence and a NOTICE) add an optional `voice` to `speak_text`, read by Kokoro and falling back to the session's voice. Nothing uses it yet, so calls sound as before: it is the base for a voice per agent. A bot hosted on your machine gets the files from its plugin (no download, still checked by sha256), the Compose, Kubernetes and Helm examples mount them like the Zoom fix (`meetings.voicePatch.configMap` in the chart), and the live meeting self-test now has a step that says a line in each of two voices.

### Upgrading

- Update the plugin with the server, as for any change to a hosted bot. The Compose and Kubernetes examples mount the new files only when you add the lines; nothing changes until you do.

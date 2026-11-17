### Changed

- **A listening agent now wakes 1 second after a question on the call, not 3.** When the last thing someone said ends in "?", an agent you let listen wakes after 1 second of quiet (`[delivery] listen_ask_s`, default 1.0; an `[delivery]` value you set yourself stays). Other talk still waits 15 seconds.

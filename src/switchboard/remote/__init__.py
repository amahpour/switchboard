"""Remote members (DESIGN.md §27): the link protocol, remotes.toml and the satellite.

- ``proto``: frames, their validators, the noise-tolerant hello, ages (no I/O).
- ``config``: ``remotes.toml`` (the desktop's remotes) and ``satellite.toml`` (a Pi home).
- ``satellite``: ``switchboard satellite``, the Pi end of one link.

The desktop end (``RemoteManager``, ``RemoteLink``, ``RemoteConn``) is
``switchboard.broker.remote``.
"""

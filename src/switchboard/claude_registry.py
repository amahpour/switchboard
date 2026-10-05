"""Claude Code's session registry file, ``<sessions_dir>/<pid>.json`` (DESIGN.md §9.2, §27.5.6).

A leaf module: the broker (its local host view and the MCP attest in
``broker/peer.py``) and a remote host's satellite read the registry through it, and
it imports nothing from the rest of the package, so the satellite loads no adapter.

``read_registry`` never blocks and never raises: it opens the file without
following a final symlink and without blocking (a FIFO swapped in for the file
can't stall an event loop), checks on that open file that it is a regular file of
this user of at most 1 MiB, and gives ``None`` for anything else, including JSON
that is too deep or too large a number for Python to parse. ``registry_status``
bounds ``statusUpdatedAt`` before it does any arithmetic with it.

``read_registry`` is a probe of a participant's process on the machine it runs on:
``tests/unit/test_host_probes_static.py`` allows it only in the host views and a
short list of functions.
"""

from __future__ import annotations

import json
import math
import os
import stat
from typing import Any

MAX_BYTES = 1 << 20
REGISTRY_IDLE = frozenset({"idle", "shell"})  # shell: turn ended, background command still runs
# statusUpdatedAt is epoch ms (seconds tolerated); anything at or past this is not a time
_MAX_STAMP = 1e15


def read_registry(sessions_dir: str | os.PathLike[str], pid: int) -> dict[str, Any] | None:
    """``<sessions_dir>/<pid>.json`` if it is a regular file owned by us and parses to an object."""
    path = os.path.join(os.path.expanduser(str(sessions_dir)), f"{int(pid)}.json")
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > MAX_BYTES:
            return None
        raw = b""
        while len(raw) <= MAX_BYTES:
            chunk = os.read(fd, MAX_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
        if len(raw) > MAX_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None
    finally:
        os.close(fd)
    return data if isinstance(data, dict) else None


def registry_status(data: dict[str, Any]) -> tuple[str | None, float | None]:
    """A registry file's ``(status, since)``: the status (None if missing or odd) and
    when it began, from ``statusUpdatedAt`` (epoch ms; seconds tolerated), else None.
    The satellite relays exactly these for a remote session (§27.5.6)."""
    status = data.get("status")
    status = status if isinstance(status, str) and len(status) <= 32 else None
    since: float | None = None
    upd = data.get("statusUpdatedAt")
    # bounded before any float arithmetic: a huge integer would overflow it
    if (
        isinstance(upd, (int, float))
        and not isinstance(upd, bool)
        and 0 < upd < _MAX_STAMP
        and math.isfinite(upd)
    ):
        since = float(upd) / 1000.0 if upd > 1e11 else float(upd)  # epoch ms (seconds tolerated)
    return status, since

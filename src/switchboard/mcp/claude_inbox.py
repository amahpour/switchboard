"""Post into the parent Claude Code session's inbox socket (DESIGN.md §6.4, FINDINGS §2, §12).

This runs **only in the ``switchboard mcp`` process**, a live child of ``claude``:
a live descendant posting with the session token skips the bypass-mode hold,
and holding the connection ~0.3 s lets Claude verify the poster's pid
(FINDINGS §2 1.4). Recipe:

1. ``{"type": "auth", "token": <CLAUDE_CODE_MESSAGING_TOKEN>}`` (optional first line);
2. ``{"type": "user", "message": {"role": "user", "content": <str>}, "from": ..., "msg_id": ...}``;
3. keep the connection open ``hold_s``, then close. The socket never replies.

``content`` is always a plain string. The frame never carries an urgency key:
an interrupting frame suppresses the running tool's PostToolUse hook and
doesn't interrupt the tool anyway (FINDINGS §2 1.2).

**Refusal guard.** ``post`` refuses unless the target was verified at startup
(local detection said claude, and ``$CLAUDE_CODE_MESSAGING_SOCKET`` equals the
``messagingSocketPath`` of ``<sessions_dir>/<ppid>.json``), the parent is still
that same process, the env still names that socket, and the path is a socket
owned by this user. The socket path never comes from the broker. The token is
read from the environment at post time and is never logged, returned or sent
to the broker.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import stat
import time
from dataclasses import dataclass

SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
TOKEN_ENV = "CLAUDE_CODE_MESSAGING_TOKEN"
MAX_TEXT = 256 * 1024
CONNECT_TIMEOUT_S = 5.0
_LABEL_RE = re.compile(r"[^a-z0-9#_.-]")


class InboxRefused(Exception):
    """The guard refused to post (not a verified Claude parent's own socket)."""


@dataclass(frozen=True)
class InboxTarget:
    """The inbox socket verified at startup, and the parent it belongs to."""

    sock: str
    ppid: int


def verify_target(
    harness: str | None, env_sock: str | None, registry_sock: str | None, ppid: int | None
) -> InboxTarget | None:
    """The startup check: detection said claude, and the env socket is the one the
    parent's registry file names. Anything else: no inbox."""
    if harness != "claude" or not env_sock or not registry_sock or not ppid:
        return None
    if env_sock != registry_sock:
        return None
    return InboxTarget(sock=env_sock, ppid=int(ppid))


def target_ok(t: InboxTarget | None) -> bool:
    """Re-checked before every post: same parent, same env socket, a socket we own."""
    if t is None or os.getppid() != t.ppid or os.environ.get(SOCKET_ENV) != t.sock:
        return False
    try:
        st = os.lstat(t.sock)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid()


def sender_label(room: str, sender: str) -> str:
    """``from``: ``switchboard:<room>/<sender>``. Self-declared; the model never sees it,
    Claude uses it as the rate-limit and dedupe key (FINDINGS §2 1.7)."""
    r = _LABEL_RE.sub("", (room or "").lower())[:33] or "#room"
    n = _LABEL_RE.sub("", (sender or "").lower())[:24] or "switchboard"
    return f"switchboard:{r}/{n}"


def message_id(batch_id: int) -> str:
    """Unique per frame (batch ids repeat across broker homes; the suffix doesn't)."""
    return f"yk-b{int(batch_id)}-{secrets.token_hex(4)}"


def frame_lines(token: str | None, text: str, from_: str, msg_id: str) -> bytes:
    lines = []
    if token:
        lines.append(json.dumps({"type": "auth", "token": token}))
    lines.append(
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": text}, "from": from_, "msg_id": msg_id}
        )
    )
    return ("\n".join(lines) + "\n").encode()


def post(
    target: InboxTarget | None, token: str | None, text: str, from_: str, msg_id: str, hold_s: float = 0.3
) -> float:
    """Send one frame and hold the connection ``hold_s``. Returns the send time (epoch s).

    Raises InboxRefused when the guard fails, ValueError on bad text, OSError on
    socket errors.
    """
    if not target_ok(target):
        raise InboxRefused("not this process's verified Claude inbox")
    assert target is not None
    if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
        raise ValueError("bad frame text")
    data = frame_lines(token, text, from_, msg_id)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT_S)
    try:
        s.connect(target.sock)
        s.sendall(data)
        t = time.time()
        if hold_s > 0:
            time.sleep(hold_s)
        return t
    finally:
        s.close()

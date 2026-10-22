"""Listener keys (DESIGN.md §49.11, #488): what lets Claude Code's helper scripts reach the server when
the agent signed in through the browser and holds no token a script can read.

Claude Code keeps a browser sign-in's token to itself, so the plugin's command hooks (``bin/listen``,
the idle wake, and ``bin/session``, the start and end of a session) have nothing to send. A plugin MCP
server may send a header made by a command, though, and that is the one thing that carries data made on
the machine into the signed-in connection. So the key is **made on the machine and claimed over the
sign-in**, never handed down it (an ``mcp_tool`` hook can't write a file, and its answer would reach the
model):

- ``bin/headers`` makes a key (``sbl_`` and 64 hex), keeps it in a 0600 file of the plugin's data folder,
  and sends only its sha256 in ``X-Switchboard-Listener`` on every request of the connection.
- ``claim``: a request that carries a live agent token (the sign-in) and that header records the hash
  against the token. Only a request with a live token can claim one, so there is no unauthenticated route
  and nobody can claim ahead of the real connection; a hash another token holds is never taken over.
- **Scope.** The key is a bearer for ``POST /mcp/listen`` and ``POST /mcp/session`` and nothing else: the
  tools, the hook tool and every other route answer only an agent token. It is for **one session**: the one
  the connection's own hooks last named (``name_session``), or, before any hook has run, the first one the
  key itself presents (pinned). Another session gets ``wrong_session``.
- **Revoked with its sign-in**: the key is only as live as its token (``revoked``), and it ages out
  after ``IDLE_S`` without use, or past ``MAX_PER_TOKEN`` newer keys on the same token.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from switchboard.broker.auth import sha256_hex
from switchboard.clock import Clock
from switchboard.models import AgentTokenRow

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.store import Store

PREFIX = "sbl_"
KEY_RE = re.compile(r"sbl_[0-9a-f]{64}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
HEADER = b"x-switchboard-listener"  # the key's sha256, on the signed-in connection's requests
IDLE_S = 7 * 24 * 3600.0  # unused this long and a key is gone: the listener's own limit (its hook's timeout)
MAX_PER_TOKEN = 16  # live keys a token may have; a connection that makes one each time pushes out the oldest
TOUCH_S = 60.0  # a key's use is written at most this often
CACHE_MAX = 4096  # what the two memories below remember at once


def key_hash(key: str) -> str:
    return sha256_hex(key)


@dataclass(frozen=True)
class Checked:
    """What ``ListenerKeys.check`` says of a key: ``token`` is the sign-in's token when it is live and
    the key is for the session; ``why`` is the word the route answers with otherwise (401 ``unknown_key``
    or ``revoked``, 403 ``wrong_session``)."""

    token: AgentTokenRow | None
    why: str = ""


class ListenerKeys:
    """The keys' rules over the store, with a small memory so a signed-in connection's every request
    (each carries the header) doesn't write each time."""

    def __init__(self, store: "Store", clock: Clock) -> None:
        self.store, self.clock = store, clock
        self._claimed: OrderedDict[tuple[int, str], float] = OrderedDict()  # (token, hash) -> when written
        self._named: OrderedDict[tuple[int, str], str] = OrderedDict()  # (token, hash) -> its session

    @staticmethod
    def hash_in(value: str | None) -> str | None:
        """The key hash a request's header carries, if it is one (64 lowercase hex)."""
        return value if value is not None and HASH_RE.fullmatch(value) else None

    def claim(self, token: AgentTokenRow, h: str) -> None:
        """A request with a live token carried this key hash: the key is the token's. Written when it is
        new and then at most once a minute (its use)."""
        now = self.clock.now()
        mark = (token.id, h)
        if now - self._claimed.get(mark, float("-inf")) < TOUCH_S:
            return
        if self.store.listener_key_claim(token.id, h, MAX_PER_TOKEN, IDLE_S):
            self._remember(self._claimed, mark, now)

    def name_session(self, token: AgentTokenRow, h: str, session_id: str) -> None:
        """A hook on the signed-in connection that carries this key hash named its session: the key is for
        that session now. A ``/clear`` or a resume in the same Claude Code makes a new session, and the
        next hook moves the key to it."""
        mark = (token.id, h)
        if self._named.get(mark) == session_id:
            return
        self.store.listener_key_name_session(token.id, h, session_id)
        self._remember(self._named, mark, session_id)

    def check(self, key: str, session_id: str | None) -> Checked:
        """Whether ``key`` may act for ``session_id`` now. Nothing is said of why a key is unknown: a
        hash nobody claimed, a key idle too long and one that never was look the same."""
        if not KEY_RE.fullmatch(key):
            return Checked(None, "unknown_key")
        got = self.store.listener_key(key_hash(key))
        if got is None:
            return Checked(None, "unknown_key")
        row, token = got
        if token.revoked_at is not None:
            return Checked(None, "revoked")
        now = self.clock.now()
        if row.last_used_at < now - IDLE_S:
            return Checked(None, "unknown_key")
        if session_id is not None:
            bound = row.session_id or self.store.listener_key_bind(row.id, session_id)
            if bound != session_id:
                return Checked(None, "wrong_session")
        if now - row.last_used_at >= TOUCH_S:
            self.store.listener_key_used(row.id)
        return Checked(token)

    @staticmethod
    def _remember(memory: "OrderedDict", mark: tuple[int, str], value: object) -> None:
        memory[mark] = value
        memory.move_to_end(mark)
        while len(memory) > CACHE_MAX:
            memory.popitem(last=False)

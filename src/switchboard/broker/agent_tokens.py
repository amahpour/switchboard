"""Agent tokens (DESIGN.md §49.1, #382): a person's token for the coding agents on one device,
which those agents present to the server over HTTPS instead of running anything of switchboard's
locally (#380).

- A token is ``sbat_`` and 43 characters of base64url (32 random bytes). It is shown once, in the
  answer that makes it; the store keeps only its sha256, as it keeps a web session's.
- Its device name is a host name (``valid_host``): the agents on that device will be members on
  that host, as a machine's are (§27.6). So it may not be the name of a machine that dials in, of
  an SSH remote, or of another live token, and a machine may not be paired under a live token's
  name. One live token per device name.
- It is for agents only: no web route, cookie or socket method takes it, so it can't sign in to
  the web UI, post as its person or run a command.
- Making one grants access, so on a hosted broker it needs a password or passkey check in the
  last five minutes, as pairing a machine does (§31.6). Revoking one, or removing its person,
  only takes access away and needs no check.
"""

from __future__ import annotations

import re
import secrets
from typing import TYPE_CHECKING, Any

from switchboard.broker.auth import sha256_hex
from switchboard.broker.service import ServiceError
from switchboard.models import AgentTokenRow, valid_host

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState

PREFIX = "sbat_"
TOKEN_RE = re.compile(r"sbat_[A-Za-z0-9_-]{43}")
MAX_PER_PERSON = 20
DEVICE_RULE = "a device's name looks like work-laptop (a-z first, then a-z, 0-9, '-'; at most 24)"


def new_token() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return sha256_hex(token)


def summary(row: AgentTokenRow) -> dict[str, Any]:
    """What the web UI lists of a token: never the token, never its hash."""
    return {
        "id": row.id,
        "device": row.device,
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
    }


def listing(state: "BrokerState", person_id: int | None) -> dict[str, Any]:
    return {
        "tokens": [summary(t) for t in state.store.agent_tokens_of(person_id)],
        "max": MAX_PER_PERSON,
    }


def device_name(value: Any) -> str:
    """A device name as typed, made canonical: trimmed and lower case, then held to ``valid_host``."""
    name = value.strip().lower() if isinstance(value, str) else ""
    if not valid_host(name):
        raise ServiceError("bad_request", DEVICE_RULE)
    return name


def make(state: "BrokerState", person_id: int | None, device: Any, by: str) -> dict[str, Any]:
    """A new token for ``person_id``'s device: its listing, and the token itself, this once. The
    caller checked the session (and, on a hosted broker, the fresh check)."""
    name = device_name(device)
    if state.remotes is not None and name in state.remotes.links:
        raise ServiceError(
            "conflict", f"{name} is a remote this broker dials over ssh (remotes.toml): pick another name"
        )
    m = state.store.machine(name)
    if m is not None and not m.removed:
        raise ServiceError("conflict", f"{name} is a machine that dials in to this broker: pick another name")
    if len(state.store.agent_tokens_of(person_id)) >= MAX_PER_PERSON:
        raise ServiceError(
            "conflict", f"you have {MAX_PER_PERSON} agent tokens already: revoke one you no longer use first"
        )
    token = new_token()
    row = state.store.agent_token_add(person_id, name, token_hash(token))
    if row is None:
        held = state.store.agent_token_for_device(name)
        if held is not None and held.person_id == person_id:
            raise ServiceError(
                "conflict", f"you have a token for {name} already: revoke it first, or pick another name"
            )
        raise ServiceError("conflict", f"{name} is taken: pick another name")
    state.store.add_event("agent_tokens", data={"what": "add", "device": name, "by": by})
    return {**summary(row), "token": token}


def revoke(state: "BrokerState", person_id: int | None, token_id: Any, by: str) -> dict[str, Any]:
    """Revoke one of ``person_id``'s own live tokens."""
    if not isinstance(token_id, int) or isinstance(token_id, bool) or not 0 < token_id < 1 << 62:
        raise ServiceError("not_found", "no such agent token")
    row = state.store.agent_token_revoke(token_id, person_id)
    if row is None:
        raise ServiceError("not_found", "no such agent token")
    # its agents' sessions end with it: "left (work-laptop's token revoked)" (§49.3)
    ended = state.remotes.end_members(row.device, "token revoked") if state.remotes is not None else 0
    state.store.add_event(
        "agent_tokens", data={"what": "revoke", "device": row.device, "by": by, "ended": ended}
    )
    return {"ok": True, "device": row.device, "ended": ended}

"""Agent tokens (DESIGN.md §49.1, #382): a person's token for the coding agents on one device,
which those agents present to the server over HTTPS instead of running anything of switchboard's
locally (#380).

- A token is ``sbat_`` and 43 characters of base64url (32 random bytes). It is shown once, in the
  answer that makes it; the store keeps only its sha256, as it keeps a web session's.
- Its device name is a host name (``valid_host``): the agents on that device are members on that
  host (§49.3). So it may not be the name of another live token: one live token per device name.
- It is for agents only: no web route, cookie or socket method takes it, so it can't sign in to
  the web UI, post as its person or run a command.
- Making one grants access, so on a hosted broker it needs a password or passkey check in the
  last five minutes (§31.6). Revoking one, or removing its person, only takes access away and
  needs no check.
- A browser sign-in (§49.9, #386) makes the same token: ``make_signin`` is ``make`` for an agent that
  proved itself with OAuth. It is one live token per device like any other, listed beside the
  typed ones with the name the agent gave itself, and revoked the same way.
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
    """What the web UI lists of a token: never the token, never its hash. A browser sign-in says so
    (``via``) and which agent it was (``client``, the agent's own claim)."""
    return {
        "id": row.id,
        "device": row.device,
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
        "via": row.via,
        "client": row.client_name,
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
    return _create(state, person_id, device, by, "token", None)


def make_signin(
    state: "BrokerState", person_id: int | None, device: Any, by: str, client_name: str
) -> dict[str, Any]:
    """The token of a browser sign-in (§49.9): what ``make`` makes, for an agent whose person
    approved it on the consent page. ``by`` is that person's name; ``client_name`` the agent's own
    claim about itself."""
    return _create(state, person_id, device, by, "oauth", client_name)


def free(state: "BrokerState", person_id: int | None, device: str) -> None:
    """Raise unless ``person_id`` can have a new live token for ``device`` now (the rules ``make``
    holds: the cap, one live token per device name), so a consent page can say so before it asks."""
    if len(state.store.agent_tokens_of(person_id)) >= MAX_PER_PERSON:
        raise ServiceError(
            "conflict", f"you have {MAX_PER_PERSON} agent tokens already: revoke one you no longer use first"
        )
    held = state.store.agent_token_for_device(device)
    if held is None:
        return
    if held.person_id == person_id:
        what = "sign-in" if held.via == "oauth" else "token"
        raise ServiceError(
            "conflict", f"you have a {what} for {device} already: revoke it first, or pick another name"
        )
    raise ServiceError("conflict", f"{device} is taken: pick another name")


def _create(
    state: "BrokerState",
    person_id: int | None,
    device: Any,
    by: str,
    via: str,
    client_name: str | None,
) -> dict[str, Any]:
    name = device_name(device)
    free(state, person_id, name)
    token = new_token()
    row = state.store.agent_token_add(person_id, name, token_hash(token), via, client_name)
    if row is None:  # lost a race for the name between the check and the insert
        free(state, person_id, name)
        raise ServiceError("conflict", f"{name} is taken: pick another name")
    state.store.add_event(
        "agent_tokens", data={"what": "add", "device": name, "by": by, "via": via, "client": client_name}
    )
    return {**summary(row), "token": token}


def revoke(state: "BrokerState", person_id: int | None, token_id: Any, by: str) -> dict[str, Any]:
    """Revoke one of ``person_id``'s own live tokens."""
    if not isinstance(token_id, int) or isinstance(token_id, bool) or not 0 < token_id < 1 << 62:
        raise ServiceError("not_found", "no such agent token")
    row = state.store.agent_token_revoke(token_id, person_id)
    if row is None:
        raise ServiceError("not_found", "no such agent token")
    # its agents' sessions end with it: "left (work-laptop's token revoked)" (§49.3)
    ended = state.agents.end_device(row.device, "token revoked")
    state.store.add_event(
        "agent_tokens", data={"what": "revoke", "device": row.device, "by": by, "ended": ended}
    )
    return {"ok": True, "device": row.device, "ended": ended}


def revoke_by_broker(state: "BrokerState", token_id: int, why: str) -> bool:
    """Revoke a live token for the broker's own reason, not a person's request: a browser sign-in's code
    was used twice (§49.9), so whoever held the first copy may be someone else. Its device's agents end
    as with ``revoke``. True if a live token was revoked."""
    row = state.store.agent_token_revoke_id(token_id)
    if row is None:
        return False
    ended = state.agents.end_device(row.device, "token revoked")
    state.store.add_event(
        "agent_tokens",
        data={"what": "revoke", "device": row.device, "by": "switchboard", "why": why, "ended": ended},
    )
    return True

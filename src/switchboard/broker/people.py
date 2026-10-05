"""People on a hosted broker (DESIGN.md §32): who a web session is, the names people may
have, and what the admin section shows.

The owner is whoever set the broker up (the claim, §31.3). Everyone else is a person the
owner added by name in the admin section, with a one-time password to hand over. Every
signed-in person is a human in every room: they post under their own name, run every
command, pair machines, and every agent takes their messages as its user's (kind=human).
Only the owner has the admin section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from switchboard.models import RESERVED_NAMES, SCREEN_NAME_RE, PersonRow

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState

ADMIN_ALIAS = "admin"  # the owner may sign in as "admin" too: the log's line says so (§32.4)
ONE_TIME_TTL_S = 7 * 24 * 3600.0  # a person's one-time password works for a week


@dataclass(frozen=True)
class Who:
    """A live web session's person: ``person_id`` None is the owner (and anyone signed in to a
    desktop broker). ``must_reset``: still on a one-time password, so the session may only
    choose its own password or passkey."""

    h: str
    person_id: int | None
    name: str
    must_reset: bool = False

    @property
    def owner(self) -> bool:
        return self.person_id is None


def who_of(state: "BrokerState", h: str | None) -> Who | None:
    if h is None:
        return None
    pid = state.store.web_session_person(h)
    if pid is None:
        return Who(h, None, state.cfg.human_name)
    p = state.store.person(pid)
    if p is None or not p.active:  # removed: their sessions are gone already; never trust one left
        state.store.web_session_delete(h)
        return None
    return Who(h, pid, p.name, p.must_reset)


def is_owner_name(state: "BrokerState", name: str) -> bool:
    n = name.strip().lower()
    return n in (state.cfg.human_name.lower(), ADMIN_ALIAS)


def account(state: "BrokerState", ident: str) -> tuple[str, "PersonRow | None"]:
    """Who signs in as ``ident`` (#192, §39): ``("owner", None)``, ``("person", row)`` or
    ``("none", None)``. An email is who someone is. A name still works, for now, for whoever
    has no email yet (the grace period); ``admin`` always means the owner."""
    n = ident.strip().lower()
    if not n:
        return "none", None
    if "@" in n:
        if n == state.store.owner_email():
            return "owner", None
        p = state.store.person_by_email(n)
        return ("person", p) if p is not None else ("none", None)
    if n == ADMIN_ALIAS or (n == state.cfg.human_name.lower() and state.store.owner_email() is None):
        return "owner", None
    p = state.store.person_named(n)
    return ("person", p) if p is not None and p.email is None else ("none", None)


def email_of(state: "BrokerState", person_id: int | None) -> str | None:
    """The email of a person, or of the owner (None), or None when they have none (#192)."""
    if person_id is None:
        return state.store.owner_email()
    p = state.store.person(person_id)
    return p.email if p is not None else None


def name_problem(state: "BrokerState", name: Any) -> str | None:
    """Why ``name`` can't be a new person's, or None. Names look like screen names (they are
    @mentioned the same way): a-z first, then a-z, 0-9, ``_`` or ``-``, at most 24."""
    if not isinstance(name, str) or not SCREEN_NAME_RE.match(name.strip().lower()):
        return "names look like bob or bob-k: a-z first, then a-z, 0-9, '_' or '-', at most 24"
    n = name.strip().lower()
    if n in RESERVED_NAMES or n.startswith("switchboard") or is_owner_name(state, n):
        return f"{n} is reserved"
    if state.store.person_named(n) is not None:
        return f"{n} is someone here already"
    return None


def names(state: "BrokerState") -> list[str]:
    """Every person's name, the owner's first: the humans in every room."""
    return [state.cfg.human_name] + [p.name for p in state.store.people()]


def password_state(p: PersonRow, now: float, google: bool = False) -> str:
    """How a person signs in, for the admin section: ``one-time`` (not used yet),
    ``expired`` (a one-time password past its week), ``password``, ``google`` (#70) or ``passkey``."""
    if p.must_reset:
        return "expired" if p.password_expires_at is not None and now >= p.password_expires_at else "one-time"
    return "password" if p.password_hash else "google" if p.email and google else "passkey"


def summary(state: "BrokerState") -> list[dict[str, Any]]:
    """The admin section's list: the owner, then everyone else in the order they were added."""
    now = state.clock.now()
    out: list[dict[str, Any]] = [
        {
            "id": None,
            "name": state.cfg.human_name,
            "admin": True,
            "passkeys": len(state.store.passkeys_of(None)),
            "password": state.store.owner_password_hash() is not None,
            "sign_in": "admin",
            "created_at": state.store.owner_claimed_at(),
            "email": state.store.owner_email(),
        }
    ]
    for p in state.store.people():
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "admin": False,
                "passkeys": len(state.store.passkeys_of(p.id)),
                "password": p.password_hash is not None and not p.must_reset,
                "sign_in": password_state(p, now, state.oidc is not None),
                "created_at": p.created_at,
                "expires_at": p.password_expires_at if p.must_reset else None,
                "email": p.email,
            }
        )
    return out


def invite_text(origin: str, name: str, one_time: str) -> str:
    """What the owner sends a new person (the admin section's Copy button)."""
    return (
        f"You're invited to switchboard: {origin}\n"
        f"Sign in as {name} with the one-time password {one_time} (it works for 7 days).\n"
        "Right after, you choose your own password, or a passkey."
    )

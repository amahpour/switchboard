"""People on a hosted broker (DESIGN.md §32): who a web session is, the names people may
have, and what the admin section shows.

The owner is whoever set the broker up (the claim, §31.3). Everyone else is a person the
owner added by name in the admin section, with a one-time password to hand over. Every
signed-in person is a human in every room: they post under their own name, run every
command, pair machines, and every agent takes their messages as its user's (kind=human).
Only the owner has the admin section.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from switchboard.models import RESERVED_NAMES, SCREEN_NAME_RE, PersonRow

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState

ADMIN_ALIAS = "admin"  # the owner may sign in as "admin" too: the log's line says so (§32.4)
ONE_TIME_TTL_S = 7 * 24 * 3600.0  # a person's one-time password works for a week
EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s.]{2,63}")
MAX_EMAIL = 254  # the column's limit (people.email), and what an address can be
MAX_NAME_PART = 64  # a first or a last name (the columns' limit, §39.6)


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


def clean_email(raw: Any) -> tuple[str | None, str | None]:
    """An email from a request (#192, §39): ``(email, None)`` lowercased, ``(None, None)`` for
    none, or ``(None, why)``. Trusted as typed: nothing is sent to it."""
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, "email must be a string or null"
    email = raw.strip().lower()
    if not email:
        return None, None
    if len(email) > MAX_EMAIL or not EMAIL_RE.fullmatch(email):
        return None, "that isn't an email address"
    return email, None


def _slug(text: str) -> str:
    """``text`` as a screen name: accents dropped (José -> jose), lowercase, spaces, ``.`` and
    ``+`` as ``-``, anything else that can't be in a name dropped, a letter first, at most 24.
    May come out empty."""
    ascii_ = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    kept = re.sub(r"[^a-z0-9_-]", "", re.sub(r"[\s.+]+", "-", ascii_))
    kept = re.sub(r"^[^a-z]+", "", kept)
    return re.sub(r"-{2,}", "-", kept)[:24].rstrip("-")


def name_from_email(email: str) -> str:
    """A name for someone added by email alone (#192): the part before the ``@``, as a screen
    name. May come out empty or taken: the caller checks it as any name (``name_problem``)."""
    return _slug(email.split("@", 1)[0])


def name_for(state: "BrokerState", first_name: str | None, last_name: str | None, email: str | None) -> str:
    """The name in the rooms for someone added with a first and last name (#192, §39.6): the
    first name (``ari``), else with the last name's initial (``ari-m``), else with a digit
    (``ari2`` to ``ari9``); from the email when there's no first name. The first that's free;
    else the plain one, which ``name_problem`` then refuses with its reason."""
    base = _slug(first_name) if first_name else name_from_email(email or "")
    tries = [base]
    if base and last_name and _slug(last_name):
        tries.append(f"{base}-{_slug(last_name)[0]}"[:24])
    tries += [f"{base[:23]}{n}" for n in range(2, 10)] if base else []
    return next((t for t in tries if name_problem(state, t) is None), base)


def clean_name_part(raw: Any) -> tuple[str | None, str | None]:
    """A first or last name from a request (§39.6): ``(name, None)`` with its spaces tidied and
    control and format characters dropped, ``(None, None)`` for none, or ``(None, why)``. Shown
    as text only (``textContent``), never as HTML."""
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, "a name must be text"
    name = " ".join("".join(c for c in raw if not unicodedata.category(c).startswith("C")).split())
    if not name:
        return None, None
    if len(name) > MAX_NAME_PART:
        return None, f"a first or last name is at most {MAX_NAME_PART} characters"
    return name, None


def full_name(first_name: str | None, last_name: str | None) -> str | None:
    """ "Ari Mahpour", "Ari", or None when neither is set."""
    return " ".join(x for x in (first_name, last_name) if x) or None


def email_of(state: "BrokerState", person_id: int | None) -> str | None:
    """The email of a person, or of the owner (None), or None when they have none (#192)."""
    if person_id is None:
        return state.store.owner_email()
    p = state.store.person(person_id)
    return p.email if p is not None else None


def names_of(state: "BrokerState", person_id: int | None) -> tuple[str | None, str | None]:
    """The first and last name of a person, or of the owner (None) (§39.6)."""
    if person_id is None:
        return state.store.owner_names()
    p = state.store.person(person_id)
    return (p.first_name, p.last_name) if p is not None else (None, None)


def full_names(state: "BrokerState") -> dict[str, str]:
    """Each person's name in the rooms -> their full name, for those who have one (the
    Members panel and the hover on their messages)."""
    out: dict[str, str] = {}
    owner = full_name(*state.store.owner_names())
    if owner:
        out[state.cfg.human_name] = owner
    for p in state.store.people():
        full = full_name(p.first_name, p.last_name)
        if full:
            out[p.name] = full
    return out


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


def rename_problem(state: "BrokerState", name: Any, person_id: int | None) -> str | None:
    """Why a person (or the owner, None) can't be renamed to ``name`` (#114, DESIGN.md §41), or
    None. As a new person's name (``name_problem``), and: no agent in any room has it; the
    owner's may not start an agent's name or the start of an agent CLI's (an agent can't join
    under a name that starts with the owner's, and its own name would read as theirs)."""
    from switchboard.config import _AGENT_PREFIXES

    if not isinstance(name, str) or not SCREEN_NAME_RE.match(name.strip().lower()):
        return "names look like bob or bob-k: a-z first, then a-z, 0-9, '_' or '-', at most 24"
    n = name.strip().lower()
    if person_id is None:
        current = state.cfg.human_name
    else:
        p = state.store.person(person_id)
        current = p.name if p is not None else ""
    if n == current:
        return None  # no change
    if n in RESERVED_NAMES or n.startswith("switchboard") or n == ADMIN_ALIAS:
        return f"{n} is reserved"
    if person_id is not None and n == state.cfg.human_name.lower():
        return f"{n} is someone here already"
    other = state.store.person_named(n)
    if other is not None and other.id != person_id:
        return f"{n} is someone here already"
    agents = state.store.active_agent_names()
    if n in agents:
        return f"an agent here is called {n}"
    if person_id is None:
        clash = next((a for a in agents if a.startswith(n)), None)
        if clash is not None:
            return f"an agent here is called {clash}, which would read as yours"
        if any(p.startswith(n) for p in _AGENT_PREFIXES):
            return f"{n} is the start of an agent's name (claude, codex, cursor, devin)"
    return None


def rename(state: "BrokerState", person_id: int | None, new: str, by: str) -> str | None:
    """Rename a person (or the owner, None) to ``new``, checked by ``rename_problem`` first
    (#114, §41): their sessions carry on under the new name, messages keep the name they were
    sent under, and every open room gets a notice. Returns the old name, or None if unchanged."""
    if person_id is None:
        old = state.cfg.human_name
    else:
        p = state.store.person(person_id)
        assert p is not None
        old = p.name
    if new == old:
        return None
    if person_id is None:
        state.store.set_owner_name(new)
        state.set_human_name(new)
    else:
        state.store.person_rename(person_id, new)
    state.store.add_event("people", data={"what": "rename", "from": old, "to": new, "by": by})
    note = f"{old} is now {new}" + ("" if by in (old, new) else f" (renamed by {by})")
    state.service.notice_everywhere(note)
    return old


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
            "first_name": state.store.owner_names()[0],
            "last_name": state.store.owner_names()[1],
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
                "first_name": p.first_name,
                "last_name": p.last_name,
            }
        )
    return out


def invite_text(
    origin: str, name: str, one_time: str, email: str | None = None, first_name: str | None = None
) -> str:
    """What the owner sends a new person (the admin section's Copy button): it greets them by
    their first name, and who they sign in as is their email when they have one (#192), else
    their name."""
    who = f"with {email} and" if email else f"as {name} with"
    hello = f"Hi {first_name}, you're invited" if first_name else "You're invited"
    return (
        f"{hello} to switchboard: {origin}\n"
        f"Sign in {who} the one-time password {one_time} (it works for 7 days).\n"
        "Right after, you choose your own password, or a passkey."
    )

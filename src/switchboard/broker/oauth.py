"""Signing an agent in through the browser (DESIGN.md §49.9, #386): OAuth 2.1 for ``/mcp``.

An agent that has no token calls ``/mcp``, is told where to start (``challenge``), reads two
documents (RFC 9728, RFC 8414), registers itself (RFC 7591), and sends its person's browser to
``/oauth/authorize``. The person signs in, if they are not, and approves on the consent page; the
browser goes back to the agent's loopback address with a one-time code, and the agent swaps it, with
its PKCE verifier, for an access token. That token **is** an agent token (§49.1): the same ``sbat_``
shape, one live per device, stored as a sha256, revoked in Settings → Agents.

This module is the rules; the routes are in ``web.py``. What it holds to:

- **PKCE ``S256`` is required**, and the only method. A code lives 60 seconds, works once, is kept
  only as a hash in memory, and is bound to the client, the exact ``redirect_uri``, the challenge, the
  person and the device. A second use of a spent code revokes the token it made.
- **A redirect is only ever to a URI the client registered, compared as a whole string**, and only
  loopback ``http://localhost|127.0.0.1|[::1]:<port>/…`` can be registered: a link someone sends
  cannot deliver a code to another host. A bad client or redirect gets an error page, never a redirect.
- **Public clients only**, with no secret, and no client ID metadata documents (the server would have
  to fetch a client's URL). Registration stores a name and the URIs and is capped and rate limited.
- **No refresh token and no expiry**: the sign-in is an agent token, which lasts until it is revoked
  (the hooks and Claude's listener hold a fixed token anyway). Both Claude Code and Codex take it.
- Everything a client says (its name, ``state``, ``resource``) is a claim, cut to a length and checked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import urllib.parse
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from switchboard.broker import agent_tokens
from switchboard.broker.auth import sha256_hex
from switchboard.broker.service import ServiceError
from switchboard.clock import Clock
from switchboard.models import OAuthClientRow

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState
    from switchboard.broker.people import Who

PRM_PATH = "/.well-known/oauth-protected-resource"
AS_PATH = "/.well-known/oauth-authorization-server"
AUTHORIZE_PATH = "/oauth/authorize"
REGISTER_PATH = "/oauth/register"
TOKEN_PATH = "/oauth/token"

CODE_TTL_S = 60.0
CODES_MAX = 256  # live codes; past this the oldest is forgotten
SPENT_KEEP_S = 600.0  # a spent code is remembered this long, to catch a second use
SPENT_MAX = 1024
CLIENT_TTL_S = 24 * 3600.0  # a registration older than this is dropped: a sign-in starts within minutes
CLIENTS_MAX = 500
REGISTER_PER_MIN = 60  # registrations from anyone, in any 60 seconds
QUERY_MAX = 2000  # an authorize request's whole query
BODY_MAX = 8192  # a registration or a token request
STATE_MAX = 512
REDIRECT_MAX = 200
REDIRECTS_MAX = 5
NAME_MAX = 80
NEXT_TTL_S = 600.0  # the cookie that brings a signed-out browser back to its request after signing in

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # base64url of a sha256, unpadded
VERIFIER_RE = re.compile(r"[A-Za-z0-9._~-]{43,128}")  # RFC 7636 §4.1
STATE_RE = re.compile(r"[\x21-\x7e]{1,%d}" % STATE_MAX)
CLIENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
CODE_RE = re.compile(r"[A-Za-z0-9_-]{20,128}")
NEXT_QUERY_RE = re.compile(r"[\x21-\x7e]{1,1500}")  # what the sealed return cookie may hold


class OAuthError(Exception):
    """An OAuth error: ``code`` and ``description`` are RFC 6749's. ``redirect_uri`` is set only for an
    error that may be sent back to the client, which is one whose client and redirect are known and
    valid; any other is shown to the person, never redirected."""

    def __init__(
        self,
        code: str,
        description: str,
        status: int = 400,
        redirect_uri: str | None = None,
        state: str | None = None,
    ):
        super().__init__(description)
        self.code = code
        self.description = description
        self.status = status
        self.redirect_uri = redirect_uri
        self.state = state


def issuer(state: "BrokerState") -> str:
    """The server's origin, as browsers and agents reach it (§30): its issuer and its resource's."""
    return state.web_origin.origin


def resource(state: "BrokerState") -> str:
    return issuer(state) + "/mcp"


def challenge(state: "BrokerState") -> bytes:
    """``WWW-Authenticate`` for a request to ``/mcp`` with no live token (RFC 9728 §5.1)."""
    return f'Bearer realm="switchboard", resource_metadata="{issuer(state)}{PRM_PATH}"'.encode()


def protected_resource(state: "BrokerState") -> dict[str, Any]:
    """RFC 9728: this MCP server, and who signs agents in for it (itself)."""
    return {
        "resource": resource(state),
        "authorization_servers": [issuer(state)],
        "bearer_methods_supported": ["header"],
        "resource_name": "switchboard",
    }


def authorization_server(state: "BrokerState") -> dict[str, Any]:
    """RFC 8414: PKCE ``S256`` only, public clients only, one grant, and no refresh token."""
    base = issuer(state)
    return {
        "issuer": base,
        "authorization_endpoint": base + AUTHORIZE_PATH,
        "token_endpoint": base + TOKEN_PATH,
        "registration_endpoint": base + REGISTER_PATH,
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


# ---------------------------------------------------------------- registration (RFC 7591)
def redirect_problem(uri: Any) -> str | None:
    """Why ``uri`` can't be registered as a redirect, or None. Only a loopback address with a port and
    a path: ``http://localhost:<port>/…``, ``http://127.0.0.1:<port>/…`` or ``http://[::1]:<port>/…``,
    with no user info, query or fragment."""
    if not isinstance(uri, str) or not uri or len(uri) > REDIRECT_MAX:
        return "a redirect URI is a string of at most %d characters" % REDIRECT_MAX
    if not re.fullmatch(r"[\x21-\x7e]+", uri) or "\\" in uri or "?" in uri or "#" in uri or "@" in uri:
        return "a redirect URI has no spaces, user info, query or fragment"
    try:
        u = urllib.parse.urlsplit(uri)
        port = u.port
    except ValueError:
        return "not a URL"
    if u.scheme != "http" or (u.hostname or "") not in LOOPBACK_HOSTS:
        return "a redirect URI is a loopback address: http://localhost, http://127.0.0.1 or http://[::1]"
    if port is None or not 1 <= port <= 65535:
        return "a loopback redirect URI names a port"
    if not u.path.startswith("/"):
        return "a redirect URI has a path"
    return None


def clean_name(value: Any) -> str:
    """A client's name for itself, as one printable line of at most ``NAME_MAX`` characters."""
    text = value if isinstance(value, str) else ""
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return text[:NAME_MAX].strip() or "an agent"


def new_client_id() -> str:
    return "sbc_" + secrets.token_urlsafe(12)


def register(state: "BrokerState", body: dict[str, Any]) -> dict[str, Any]:
    """RFC 7591: record a public client with loopback redirect URIs. The answer is what was stored
    (other metadata is ignored, never kept); it has no secret."""
    if not state.oauth.allow_register():
        raise OAuthError("temporarily_unavailable", "too many registrations: try again in a minute", 429)
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= REDIRECTS_MAX:
        raise OAuthError("invalid_redirect_uri", "redirect_uris is a list of 1 to %d URIs" % REDIRECTS_MAX)
    seen: list[str] = []
    for uri in uris:
        why = redirect_problem(uri)
        if why is not None:
            raise OAuthError("invalid_redirect_uri", why)
        if uri not in seen:
            seen.append(uri)
    method = body.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise OAuthError("invalid_client_metadata", "only public clients: token_endpoint_auth_method is none")
    grants = body.get("grant_types", ["authorization_code"])
    if (
        not isinstance(grants, list)
        or "authorization_code" not in grants
        or any(g not in ("authorization_code", "refresh_token") for g in grants)
    ):
        raise OAuthError("invalid_client_metadata", "grant_types is authorization_code (and refresh_token)")
    types = body.get("response_types", ["code"])
    if not isinstance(types, list) or types != ["code"]:
        raise OAuthError("invalid_client_metadata", "response_types is code")
    name = clean_name(body.get("client_name"))
    row = state.store.oauth_client_add(new_client_id(), name, seen, CLIENTS_MAX, CLIENT_TTL_S)
    return {
        "client_id": row.client_id,
        "client_id_issued_at": int(row.created_at),
        "client_name": row.client_name,
        "redirect_uris": row.redirect_uris,
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


# ---------------------------------------------------------------- the authorization request
@dataclass(frozen=True)
class AuthRequest:
    """An authorization request that passed every check: a registered client, one of its redirect URIs,
    a PKCE challenge, and the client's ``state`` (echoed, never read)."""

    client: OAuthClientRow
    redirect_uri: str
    state: str | None
    code_challenge: str


def parse_authorize(state: "BrokerState", items: list[tuple[str, str]]) -> AuthRequest:
    """Check an authorize request's query (RFC 6749 §4.1.1, RFC 7636, RFC 8707). Anything wrong about
    the client or the redirect is shown to the person (``OAuthError`` with no ``redirect_uri``);
    anything else is an error the client can be told, so it stops waiting."""
    if sum(len(k) + len(v) for k, v in items) > QUERY_MAX:
        raise OAuthError("invalid_request", "the request is too long")
    q: dict[str, str] = {}
    for k, v in items:
        if k in q:
            raise OAuthError("invalid_request", f"{k[:40]} was sent twice")
        q[k] = v
    client_id = q.get("client_id", "")
    row = state.store.oauth_client(client_id) if CLIENT_ID_RE.fullmatch(client_id) else None
    if row is None:
        raise OAuthError(
            "invalid_request",
            "this sign-in isn't one this switchboard knows: it may have expired."
            " Start it again from the agent.",
        )
    redirect = q.get("redirect_uri", "")
    if redirect not in row.redirect_uris:
        raise OAuthError("invalid_request", "the agent's address for the answer isn't the one it registered")
    echo = q.get("state")
    echo = echo if echo is not None and STATE_RE.fullmatch(echo) else None

    def fail(code: str, description: str) -> OAuthError:
        return OAuthError(code, description, redirect_uri=redirect, state=echo)

    if "state" in q and echo is None:
        raise fail("invalid_request", "state is at most %d printable characters" % STATE_MAX)
    if q.get("response_type") != "code":
        raise fail("unsupported_response_type", "response_type is code")
    if q.get("code_challenge_method") != "S256":
        raise fail("invalid_request", "PKCE with the S256 method is required")
    challenge_ = q.get("code_challenge", "")
    if not CHALLENGE_RE.fullmatch(challenge_):
        raise fail("invalid_request", "code_challenge is the unpadded base64url of a SHA-256")
    if "resource" in q and q["resource"] != resource(state):
        raise fail("invalid_target", "resource is this switchboard's /mcp")
    return AuthRequest(row, redirect, echo, challenge_)


def redirect_with(uri: str, **params: str | None) -> str:
    """``uri`` (a registered one, which has no query) with the parameters that are not None."""
    return uri + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


def error_redirect(err: OAuthError) -> str:
    assert err.redirect_uri is not None
    return redirect_with(err.redirect_uri, error=err.code, error_description=err.description, state=err.state)


def denied_redirect(req: AuthRequest) -> str:
    return redirect_with(req.redirect_uri, error="access_denied", state=req.state)


def consent_view(state: "BrokerState", req: AuthRequest, who: "Who") -> dict[str, Any]:
    """What the consent page shows (§49.9): who is signing in, what the agent calls itself, where its
    answer goes, and whether a password or passkey check is still wanted before Approve."""
    from switchboard.broker import people  # late: people imports the broker state

    return {
        "client_name": req.client.client_name,
        "redirect_to": urllib.parse.urlsplit(req.redirect_uri).netloc,
        "person": who.name,
        "email": people.email_of(state, who.person_id),
        "hosted": state.hosted,
        "fresh": (not state.hosted) or state.fresh_check(who.h),
        "password": bool(
            state.store.owner_password_hash() is not None
            if who.person_id is None
            else getattr(state.store.person(who.person_id), "password_hash", None)
        ),
        "passkeys": len(state.store.passkeys_of(who.person_id)),
        "device_rule": agent_tokens.DEVICE_RULE,
    }


def approve(state: "BrokerState", req: AuthRequest, who: "Who", device: Any) -> str:
    """The person said yes: check the device name the way Make a token does, make the one-time code,
    and return the address to send the browser to. The caller checked the session and, on a hosted
    broker, the fresh check."""
    name = agent_tokens.device_name(device)
    agent_tokens.free(state, who.person_id, name)
    code = state.oauth.issue(req, who.person_id, name, who.name)
    state.store.add_event(
        "agent_tokens",
        data={"what": "approve", "device": name, "by": who.name, "client": req.client.client_name},
    )
    return redirect_with(req.redirect_uri, code=code, state=req.state)


# ---------------------------------------------------------------- codes, and the token endpoint
@dataclass
class CodeRecord:
    client_id: str
    client_name: str
    redirect_uri: str
    code_challenge: str
    person_id: int | None
    device: str
    by: str
    expires: float


class OAuthState:
    """What the broker remembers between an approval and a token request, in memory only: the live
    codes (by hash), the spent ones (to catch a second use), and when registrations came."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self._live: OrderedDict[str, CodeRecord] = OrderedDict()
        self._spent: OrderedDict[str, tuple[int, float]] = OrderedDict()  # hash -> token id, forget at
        self._registered: deque[float] = deque()

    def allow_register(self) -> bool:
        now = self.clock.now()
        while self._registered and now - self._registered[0] >= 60.0:
            self._registered.popleft()
        if len(self._registered) >= REGISTER_PER_MIN:
            return False
        self._registered.append(now)
        return True

    def _purge(self) -> None:
        now = self.clock.now()
        for h in [h for h, r in self._live.items() if r.expires <= now]:
            del self._live[h]
        for h in [h for h, (_, until) in self._spent.items() if until <= now]:
            del self._spent[h]

    def issue(self, req: AuthRequest, person_id: int | None, device: str, by: str) -> str:
        """A new code for an approved request: 32 random bytes, kept only as their sha256."""
        self._purge()
        while len(self._live) >= CODES_MAX:
            self._live.popitem(last=False)
        code = secrets.token_urlsafe(32)
        self._live[sha256_hex(code)] = CodeRecord(
            req.client.client_id,
            req.client.client_name,
            req.redirect_uri,
            req.code_challenge,
            person_id,
            device,
            by,
            self.clock.now() + CODE_TTL_S,
        )
        return code

    def take(self, code: str) -> CodeRecord | None:
        """The record of a live code, which is spent by being asked for (a failed exchange burns it
        too: a code is one try). None for an unknown or expired one."""
        self._purge()
        return self._live.pop(sha256_hex(code), None)

    def spend(self, code: str, token_id: int) -> None:
        """Remember that ``code`` made token ``token_id``, so a second use revokes it."""
        while len(self._spent) >= SPENT_MAX:
            self._spent.popitem(last=False)
        self._spent[sha256_hex(code)] = (token_id, self.clock.now() + SPENT_KEEP_S)

    def reused(self, code: str) -> int | None:
        """The token a spent code made, when it is presented again."""
        self._purge()
        hit = self._spent.get(sha256_hex(code))
        return hit[0] if hit else None


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def exchange(state: "BrokerState", form: dict[str, str]) -> dict[str, Any]:
    """RFC 6749 §4.1.3 with PKCE: a live code, the verifier of its challenge, the client and redirect
    it was made for, and the token of the device the person named. Raises ``OAuthError``."""
    if form.get("grant_type") != "authorization_code":
        raise OAuthError("unsupported_grant_type", "grant_type is authorization_code")
    code, verifier = form.get("code", ""), form.get("code_verifier", "")
    client_id, redirect = form.get("client_id", ""), form.get("redirect_uri", "")
    if not CODE_RE.fullmatch(code) or not VERIFIER_RE.fullmatch(verifier):
        raise OAuthError("invalid_request", "code and code_verifier are required")
    if "resource" in form and form["resource"] != resource(state):
        raise OAuthError("invalid_target", "resource is this switchboard's /mcp")
    oauth = state.oauth
    rec = oauth.take(code)
    if rec is None:
        earlier = oauth.reused(code)
        if earlier is not None:
            agent_tokens.revoke_by_broker(state, earlier, "its code was used twice")
        raise OAuthError("invalid_grant", "the code is unknown, expired or already used")
    # from here the code is spent whatever happens: it was one try
    same_client = hmac.compare_digest(rec.client_id, client_id)
    if not (same_client and hmac.compare_digest(rec.redirect_uri, redirect)):
        raise OAuthError("invalid_grant", "the code was made for another client or address")
    if not hmac.compare_digest(s256(verifier), rec.code_challenge):
        raise OAuthError("invalid_grant", "the verifier doesn't match the challenge")
    if rec.person_id is not None:
        person = state.store.person(rec.person_id)
        if person is None or not person.active:
            raise OAuthError("invalid_grant", "the person who approved this is no longer here")
    try:
        made = agent_tokens.make_signin(state, rec.person_id, rec.device, rec.by, rec.client_name)
    except ServiceError as e:  # the name was taken, or the cap reached, since the approval
        raise OAuthError("invalid_grant", e.message) from None
    oauth.spend(code, made["id"])
    return {"access_token": made["token"], "token_type": "Bearer"}

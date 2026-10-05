"""Sign in with Google, over OpenID Connect, on a hosted broker (#70, DESIGN.md §38).

The authorization-code flow with PKCE, a ``state`` and a ``nonce``. The ID token comes back
from the token endpoint over TLS, and is still checked as if it hadn't: its RS256 signature
against the issuer's published keys, then ``iss``, ``aud``, ``exp``, ``iat``, the ``nonce`` and
``email_verified``. Only a person the admin added, matched by the Google email the admin set
for them, gets a session (the decision on #70); nobody else, whatever their account.

Built for any OpenID Connect issuer (Google first): the issuer's discovery document gives the
endpoints. Nothing here runs until both ``SWITCHBOARD_OIDC_CLIENT_ID`` and its secret are set.
The secret is read from the environment (or a file named by ``..._FILE``, a Kubernetes Secret
mounted as a file) and is never stored, logged or sent anywhere but the token endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

GOOGLE = "https://accounts.google.com"
CALLBACK_PATH = "/auth/oidc/callback"
FLOW_TTL_S = 600.0  # a sign-in started and not finished within 10 minutes is forgotten
MAX_FLOWS = 200  # pending sign-ins kept at once: the oldest go first
KEYS_TTL_S = 3600.0  # the issuer's keys, re-read hourly (and at once for an unknown key id)
CLOCK_SKEW_S = 120.0
MAX_BODY = 256 * 1024  # what we read back from the issuer, at most
HTTP_TIMEOUT_S = 10.0

# (method, url, form or None) -> parsed JSON; tests replace it with a fake issuer
Fetch = Callable[[str, str, dict[str, str] | None], dict[str, Any]]


class OidcError(Exception):
    """A sign-in that failed. ``code`` goes to the sign-in page; the detail only to the log."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)

    @property
    def provider(self) -> str:
        return "google" if self.issuer == GOOGLE else "sso"


def from_env(env: dict[str, str] | None = None) -> OidcConfig | None:
    """The configuration, or None (no Google sign-in) unless both the client ID and its secret
    are set. ``SWITCHBOARD_OIDC_ISSUER`` points at another provider (https only)."""
    env = dict(os.environ) if env is None else env
    client_id = env.get("SWITCHBOARD_OIDC_CLIENT_ID", "").strip()
    secret = env.get("SWITCHBOARD_OIDC_CLIENT_SECRET", "").strip()
    secret_file = env.get("SWITCHBOARD_OIDC_CLIENT_SECRET_FILE", "").strip()
    if not secret and secret_file:
        try:
            with open(secret_file, encoding="utf-8") as f:
                secret = f.read(4096).strip()
        except OSError:
            secret = ""
    if not client_id or not secret:
        return None
    issuer = env.get("SWITCHBOARD_OIDC_ISSUER", "").strip().rstrip("/") or GOOGLE
    if not issuer.startswith("https://"):
        raise ValueError("SWITCHBOARD_OIDC_ISSUER must be an https:// URL")
    return OidcConfig(issuer=issuer, client_id=client_id, client_secret=secret)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def http_fetch(method: str, url: str, form: dict[str, str] | None) -> dict[str, Any]:
    """The real fetch: https only, the OS's trust store, a timeout and a size cap."""
    if not url.startswith("https://"):
        raise OidcError("provider", f"refusing a non-https URL from the issuer: {url[:80]}")
    import truststore

    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S, context=ctx) as r:
            body = r.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        raise OidcError("provider", f"{method} {url[:80]}: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise OidcError("provider", f"{method} {url[:80]}: {e}") from None
    if len(body) > MAX_BODY:
        raise OidcError("provider", f"{url[:80]}: answer too large")
    try:
        out = json.loads(body)
    except ValueError:
        raise OidcError("provider", f"{url[:80]}: not JSON") from None
    if not isinstance(out, dict):
        raise OidcError("provider", f"{url[:80]}: not a JSON object")
    return out


@dataclass
class _Flow:
    nonce: str
    verifier: str
    binding: str  # the sha256 of the browser cookie that started it
    at: float


class OidcClient:
    def __init__(self, cfg: OidcConfig, fetch: Fetch = http_fetch, now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.fetch = fetch
        self.now = now
        self._discovery: dict[str, Any] | None = None
        self._keys: dict[str, Any] = {}
        self._keys_at = 0.0
        self._flows: dict[str, _Flow] = {}

    # ----------------------------------------------------------- discovery
    def discovery(self) -> dict[str, Any]:
        if self._discovery is None:
            d = self.fetch("GET", self.cfg.issuer + "/.well-known/openid-configuration", None)
            for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not isinstance(d.get(k), str) or not d[k].startswith("https://"):
                    raise OidcError("provider", f"discovery has no https {k}")
            if d.get("issuer") != self.cfg.issuer:
                raise OidcError("provider", "discovery names another issuer")
            self._discovery = d
        return self._discovery

    def _key(self, kid: str) -> rsa.RSAPublicKey:
        if kid not in self._keys or self.now() - self._keys_at > KEYS_TTL_S:
            jwks = self.fetch("GET", self.discovery()["jwks_uri"], None)
            keys: dict[str, Any] = {}
            for k in jwks.get("keys", []) if isinstance(jwks.get("keys"), list) else []:
                if isinstance(k, dict) and k.get("kty") == "RSA" and isinstance(k.get("kid"), str):
                    try:
                        n = int.from_bytes(_b64u_decode(k["n"]), "big")
                        e = int.from_bytes(_b64u_decode(k["e"]), "big")
                        keys[k["kid"]] = rsa.RSAPublicNumbers(e, n).public_key()
                    except (KeyError, ValueError, TypeError):
                        continue
            self._keys, self._keys_at = keys, self.now()
        if kid not in self._keys:
            raise OidcError("token", "signed with a key the issuer doesn't publish")
        return self._keys[kid]

    # ------------------------------------------------------------- the flow
    def start(self, redirect_uri: str, binding: str) -> tuple[str, str]:
        """``(the URL to send the browser to, state)``. ``binding``: the sha256 of a random
        cookie set on this browser now, so a callback from another browser is refused."""
        now = self.now()
        for k in [k for k, f in self._flows.items() if now - f.at > FLOW_TTL_S]:
            del self._flows[k]
        while len(self._flows) >= MAX_FLOWS:
            del self._flows[next(iter(self._flows))]
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        self._flows[state] = _Flow(nonce=nonce, verifier=verifier, binding=binding, at=now)
        challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.cfg.client_id,
                "redirect_uri": redirect_uri,
                "scope": "openid email",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return self.discovery()["authorization_endpoint"] + "?" + query, state

    def finish(self, state: str, code: str, redirect_uri: str, binding: str) -> str:
        """The verified, lowercased email of whoever signed in, or OidcError. One use per state."""
        flow = self._flows.pop(state, None) if isinstance(state, str) else None
        if flow is None or self.now() - flow.at > FLOW_TTL_S:
            raise OidcError("expired", "unknown or expired state")
        if not secrets.compare_digest(flow.binding, binding):
            raise OidcError("expired", "started in another browser")
        if not isinstance(code, str) or not 0 < len(code) <= 2048:
            raise OidcError("denied", "no code")
        tokens = self.fetch(
            "POST",
            self.discovery()["token_endpoint"],
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "code_verifier": flow.verifier,
            },
        )
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise OidcError("token", "no id_token")
        claims = self.verify(id_token, flow.nonce)
        return str(claims["email"]).strip().lower()

    def verify(self, id_token: str, nonce: str) -> dict[str, Any]:
        """Check an ID token: RS256 signature by a published key, then its claims."""
        parts = id_token.split(".")
        if len(parts) != 3 or len(id_token) > 16 * 1024:
            raise OidcError("token", "not a JWT")
        try:
            header = json.loads(_b64u_decode(parts[0]))
            claims = json.loads(_b64u_decode(parts[1]))
            sig = _b64u_decode(parts[2])
        except ValueError:
            raise OidcError("token", "not a JWT") from None
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise OidcError("token", "not a JWT")
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise OidcError("token", f"unexpected alg {str(header.get('alg'))[:20]}")
        try:
            self._key(header["kid"]).verify(
                sig, (parts[0] + "." + parts[1]).encode(), padding.PKCS1v15(), hashes.SHA256()
            )
        except InvalidSignature:
            raise OidcError("token", "bad signature") from None
        now = self.now()
        issuers = {self.cfg.issuer, self.cfg.issuer.removeprefix("https://")}  # Google sends either
        aud = claims.get("aud")
        if claims.get("iss") not in issuers:
            raise OidcError("token", "another issuer")
        if not (aud == self.cfg.client_id or (isinstance(aud, list) and self.cfg.client_id in aud)):
            raise OidcError("token", "another audience")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.cfg.client_id:
            raise OidcError("token", "another authorized party")
        exp, iat = claims.get("exp"), claims.get("iat")
        if not isinstance(exp, (int, float)) or now > exp + CLOCK_SKEW_S:
            raise OidcError("token", "expired")
        if not isinstance(iat, (int, float)) or iat > now + CLOCK_SKEW_S:
            raise OidcError("token", "issued in the future")
        if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce):
            raise OidcError("token", "nonce mismatch")
        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email or len(email) > 254:
            raise OidcError("no_email", "no email")
        if claims.get("email_verified") is not True:
            raise OidcError("unverified", "email not verified")
        return claims

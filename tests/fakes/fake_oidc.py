"""A fake OpenID Connect issuer (Google's shape) for the Sign in with Google tests (#70).

It answers ``OidcClient.fetch`` in-process: the discovery document, its keys and the token
endpoint. ``authorize`` plays the browser's trip to the provider: it reads the start URL's
``state``, ``nonce`` and PKCE challenge and returns a code. The token endpoint checks the
client secret, the redirect URI and the PKCE verifier, then returns an RS256 ID token, which
the test can spoil (another key, audience, issuer, nonce, an unverified email, an expired one).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ISSUER = "https://accounts.google.com"
CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-secret-not-real"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def sign(key: rsa.RSAPrivateKey, header: dict[str, Any], claims: dict[str, Any]) -> str:
    head = b64u(json.dumps(header).encode()) + "." + b64u(json.dumps(claims).encode())
    return head + "." + b64u(key.sign(head.encode(), padding.PKCS1v15(), hashes.SHA256()))


class FakeIssuer:
    def __init__(self, now: Any = time.time) -> None:
        self.key = new_key()
        self.kid = "k1"
        self.now = now
        self.codes: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.spoil: dict[str, Any] = {}  # claim overrides, or "key": another key, "alg": ...

    # ------------------------------------------------------------ OidcClient.fetch
    def fetch(self, method: str, url: str, form: dict[str, str] | None) -> dict[str, Any]:
        self.calls.append((method, url))
        if url == ISSUER + "/.well-known/openid-configuration":
            return {
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/o/oauth2/v2/auth",
                "token_endpoint": "https://oauth2.googleapis.com/token",
                "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
            }
        if url == "https://www.googleapis.com/oauth2/v3/certs":
            nums = self.key.public_key().public_numbers()
            return {
                "keys": [
                    {
                        "kty": "RSA",
                        "kid": self.kid,
                        "alg": "RS256",
                        "n": b64u(nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")),
                        "e": b64u(nums.e.to_bytes(3, "big")),
                    }
                ]
            }
        if url == "https://oauth2.googleapis.com/token" and method == "POST" and form:
            return self.token(form)
        raise AssertionError(f"unexpected fetch {method} {url}")

    def token(self, form: dict[str, str]) -> dict[str, Any]:
        from switchboard.broker.oidc import OidcError

        grant = self.codes.pop(form.get("code", ""), None)
        if grant is None or form.get("client_secret") != CLIENT_SECRET or form.get("client_id") != CLIENT_ID:
            raise OidcError("provider", "HTTP 400")
        if form.get("redirect_uri") != grant["redirect_uri"]:
            raise OidcError("provider", "HTTP 400: redirect_uri_mismatch")
        challenge = b64u(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
        if challenge != grant["challenge"]:
            raise OidcError("provider", "HTTP 400: invalid_grant (PKCE)")
        now = int(self.now())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "1234567890",
            "email": grant["email"],
            "email_verified": True,
            "iat": now,
            "exp": now + 3600,
            "nonce": grant["nonce"],
        }
        header = {"alg": self.spoil.get("alg", "RS256"), "kid": self.kid, "typ": "JWT"}
        key = self.spoil.get("key", self.key)
        claims.update({k: v for k, v in self.spoil.items() if k not in ("key", "alg")})
        return {"id_token": sign(key, header, claims), "access_token": "x", "token_type": "Bearer"}

    # ------------------------------------------------------------ the browser's trip
    def authorize(self, start_url: str, email: str) -> tuple[str, str]:
        """``(state, code)`` as Google would redirect back with, for the signed-in ``email``."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start_url).query))
        assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID and "openid" in q["scope"]
        assert q["code_challenge_method"] == "S256" and q["nonce"] and q["state"]
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "email": email,
            "nonce": q["nonce"],
            "challenge": q["code_challenge"],
            "redirect_uri": q["redirect_uri"],
        }
        return q["state"], code

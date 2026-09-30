"""A hosted broker's owner: the claim link and passkeys (DESIGN.md §31.3, §31.4).

A broker behind a public URL (§30) has no terminal to run ``switchboard login`` in.
Instead, while it has no owner, it prints one claim link in its log; opening it and
creating a passkey claims the broker, and from then on only the owner's passkeys sign
in. Everything WebAuthn is done by ``python-fido2``'s ``Fido2Server``; this module holds
what sits around it:

- ``ClaimTokens``: the one claim token at a time: 32 random bytes whose SHA-256 alone is
  kept, compared in constant time, spent by the claim, rotated every ``CLAIM_TTL_S``
  while the broker stays unclaimed. The claim ceremony (a registration in progress) is
  bound to the token, one at a time, for ``CEREMONY_TTL_S``.
- ``Sealer``: the sign-in ceremony's state (the challenge, the user-verification
  requirement) travels in a cookie sealed with an HMAC key the broker makes at start,
  with its expiry inside: nothing is kept on the server before a sign-in succeeds.
  ``UsedChallenges`` remembers a challenge once it has been *used*, until it would have
  expired, so a sealed cookie can't sign in twice. Only successful sign-ins can add to
  it, so an anonymous caller can't fill memory.
- ``WebAuthn``: the relying party (the public URL's host), the registration and sign-in
  options and their verification, and the sign-count rule (WebAuthn §7.2).
- ``passkeys_unavailable``: why passkeys can't work at an origin (no secure context, an
  IP address), in which case the broker prints no claim link and says so in its log.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any

from switchboard.broker.auth import WebOrigin
from switchboard.clock import Clock, SystemClock

CLAIM_TTL_S = 3600.0  # a claim link lives an hour, then a fresh one is printed
CEREMONY_TTL_S = 300.0  # a registration or sign-in ceremony (the challenge's life)
FRESH_CHECK_S = 300.0  # a passkey check this recent lets a session add a passkey or pair a machine
CLAIM_GRACE_S = 600.0  # after the claim, the backup-passkey step needs no second check
MAX_NAME = 40
RP_NAME = "switchboard"
# algorithms offered, in order of preference: ES256, EdDSA, RS256 (COSE identifiers)
ALGORITHMS = (-7, -8, -257)
MAX_CREDENTIAL = 64 * 1024  # a credential JSON from the browser, at most


class ClaimTokens:
    """The claim link's token: one at a time, in memory, hashed."""

    def __init__(self, clock: Clock | None = None, ttl_s: float = CLAIM_TTL_S):
        self.clock = clock or SystemClock()
        self.ttl_s = ttl_s
        self._hash: bytes | None = None
        self._expires = 0.0
        self.issued = 0  # links printed since the broker started
        # the ceremony bound to the token: its id (the setup cookie's value, hashed), the
        # Fido2Server state, and its deadline
        self._ceremony: tuple[bytes, Any, float] | None = None

    def mint(self) -> str:
        """A fresh token; the old one dies."""
        tok = secrets.token_urlsafe(32)
        self._hash = hashlib.sha256(tok.encode()).digest()
        self._expires = self.clock.now() + self.ttl_s
        self._ceremony = None
        self.issued += 1
        return tok

    @property
    def active(self) -> bool:
        return self._hash is not None and self.clock.now() < self._expires

    def expires_in_s(self) -> float:
        return max(0.0, self._expires - self.clock.now()) if self._hash is not None else 0.0

    def check(self, token: Any) -> bool:
        """Whether ``token`` is the live one (constant time); nothing is spent."""
        if not isinstance(token, str) or not 20 <= len(token) <= 256 or not self.active:
            return False
        assert self._hash is not None
        return hmac.compare_digest(hashlib.sha256(token.encode()).digest(), self._hash)

    def spend(self) -> None:
        """The claim succeeded: no token, no ceremony, ever again from this object."""
        self._hash = None
        self._expires = 0.0
        self._ceremony = None

    # ------------------------------------------------------------ ceremony
    def bind(self, cid: str, state: Any) -> None:
        """Bind the token to one registration ceremony, ``cid`` (the setup cookie) with the
        server's ``state``, for ``CEREMONY_TTL_S``."""
        self._ceremony = (hashlib.sha256(cid.encode()).digest(), state, self.clock.now() + CEREMONY_TTL_S)

    def ceremony_busy(self, cid: str | None) -> bool:
        """Another browser's ceremony is under way (bound and not expired, and ``cid`` isn't
        its): a second one can't start with this token meanwhile."""
        c = self._ceremony
        if c is None or self.clock.now() >= c[2]:
            return False
        if cid is None:
            return True
        return not hmac.compare_digest(hashlib.sha256(cid.encode()).digest(), c[0])

    def ceremony(self, cid: str | None) -> Any | None:
        """The bound ceremony's state for ``cid``, if it is the one and still alive."""
        c = self._ceremony
        if c is None or not isinstance(cid, str) or not cid or self.clock.now() >= c[2]:
            return None
        if not hmac.compare_digest(hashlib.sha256(cid.encode()).digest(), c[0]):
            return None
        return c[1]

    def drop_ceremony(self) -> None:
        """A failed registration: the token stays usable, the ceremony is over."""
        self._ceremony = None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Sealer:
    """Seals a small JSON state into an opaque string with an HMAC and an expiry inside,
    with a key made at start (memory only). ``unseal`` returns the state, or None when the
    seal is wrong, the string is malformed or the state has expired."""

    def __init__(self, clock: Clock | None = None, key: bytes | None = None):
        self.clock = clock or SystemClock()
        self.key = key or secrets.token_bytes(32)

    def seal(self, state: dict[str, Any], ttl_s: float) -> str:
        body = json.dumps({"s": state, "exp": self.clock.now() + ttl_s}, separators=(",", ":"), sort_keys=True).encode()
        mac = hmac.new(self.key, body, hashlib.sha256).digest()
        return _b64(body) + "." + _b64(mac)

    def unseal(self, sealed: Any) -> dict[str, Any] | None:
        if not isinstance(sealed, str) or len(sealed) > 4096 or "." not in sealed:
            return None
        b, m = sealed.rsplit(".", 1)
        try:
            body, mac = _unb64(b), _unb64(m)
        except (ValueError, TypeError):
            return None
        if not hmac.compare_digest(hmac.new(self.key, body, hashlib.sha256).digest(), mac):
            return None
        try:
            obj = json.loads(body)
        except ValueError:
            return None
        if (not isinstance(obj, dict) or not isinstance(obj.get("s"), dict)
                or not isinstance(obj.get("exp"), (int, float))):
            return None
        if self.clock.now() >= float(obj["exp"]):
            return None
        return obj["s"]


class UsedChallenges:
    """Challenges a sign-in has used, kept until they would have expired anyway."""

    def __init__(self, clock: Clock | None = None):
        self.clock = clock or SystemClock()
        self._used: dict[str, float] = {}

    def add(self, challenge: str, ttl_s: float = CEREMONY_TTL_S) -> bool:
        """False if it was used already (nothing changes then)."""
        self.purge()
        if challenge in self._used:
            return False
        self._used[challenge] = self.clock.now() + ttl_s
        return True

    def purge(self) -> None:
        now = self.clock.now()
        for k in [k for k, exp in self._used.items() if exp <= now]:
            del self._used[k]

    def __len__(self) -> int:
        return len(self._used)


def passkeys_unavailable(origin: WebOrigin) -> str | None:
    """Why passkeys can't work at ``origin`` (so there is no claim flow), or None."""
    if not origin.public:
        return "no public URL: this broker is signed in to with `switchboard login`"
    if not origin.secure_context():
        return (f"{origin.origin} is not a secure context (https, or plain http on localhost and *.localhost"
                " only), which passkeys need")
    name = origin.hostname
    if name.replace(".", "").isdigit():
        return f"{origin.origin} names an IP address, and a passkey's relying party must be a DNS name"
    return None


def clean_name(name: Any, default: str = "passkey") -> str:
    """A passkey's name as the owner typed it: one line, printable, at most ``MAX_NAME``."""
    if not isinstance(name, str):
        return default
    out = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n\t")
    out = " ".join(out.split())[:MAX_NAME]
    return out or default


def sign_count_ok(stored: int, received: int) -> bool:
    """WebAuthn §7.2 step 21: unless both counters are 0 (many platform authenticators never
    count), the received count must be greater than the stored one. Equal non-zero counts
    are refused too: a cloned authenticator would send the same count twice."""
    if stored == 0 and received == 0:
        return True
    return received > stored


@dataclass(frozen=True)
class Registered:
    """What a verified registration gives the store."""

    credential_id: bytes
    public_key: bytes  # COSE, CBOR-encoded
    aaguid: str | None
    sign_count: int


class WebAuthn:
    """The relying party for one public origin (§31.3): ``rp.id`` is the host without its
    port, ``rp.name`` is ``switchboard``, and the origin must match exactly."""

    def __init__(self, origin: WebOrigin):
        from fido2.server import Fido2Server
        from fido2.webauthn import AttestationConveyancePreference, PublicKeyCredentialRpEntity

        self.origin = origin
        self.rp_id = origin.hostname
        self.server = Fido2Server(PublicKeyCredentialRpEntity(id=self.rp_id, name=RP_NAME),
                                  attestation=AttestationConveyancePreference.NONE,
                                  verify_origin=lambda o: o == origin.origin)
        from fido2.webauthn import PublicKeyCredentialParameters, PublicKeyCredentialType

        self.server.allowed_algorithms = [
            PublicKeyCredentialParameters(type=PublicKeyCredentialType.PUBLIC_KEY, alg=alg) for alg in ALGORITHMS]

    # ------------------------------------------------------------ registration
    def register_options(self, handle: bytes, user_name: str,
                         exclude: list[bytes]) -> tuple[dict[str, Any], dict[str, Any]]:
        """The creation options (JSON, as the browser's ``PublicKeyCredential.parseCreationOptionsFromJSON``
        takes them) and the server state to verify the answer with. A discoverable credential
        with user verification, attestation ``none``, and the existing passkeys excluded."""
        from fido2.webauthn import (
            PublicKeyCredentialDescriptor,
            PublicKeyCredentialType,
            PublicKeyCredentialUserEntity,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )

        user = PublicKeyCredentialUserEntity(id=handle, name=user_name, display_name=user_name)
        descriptors = [PublicKeyCredentialDescriptor(type=PublicKeyCredentialType.PUBLIC_KEY, id=cid)
                       for cid in exclude]
        options, state = self.server.register_begin(
            user, descriptors or None, resident_key_requirement=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED)
        return _json(options), _state(state)

    def register_finish(self, state: dict[str, Any], credential: Any) -> Registered:
        """Verify a registration (the challenge, the origin, the RP id hash, user presence
        and verification) and return what to store. Raises ValueError with a short reason."""
        from fido2 import cbor
        from fido2.webauthn import UserVerificationRequirement

        cred = _credential(credential)
        st = {"challenge": state["challenge"], "user_verification": UserVerificationRequirement(state["uv"])}
        auth_data = self.server.register_complete(st, cred)
        cd = auth_data.credential_data
        if cd is None:  # pragma: no cover - register_complete asserts it
            raise ValueError("no credential in the registration")
        if cd.public_key.get(3) not in ALGORITHMS:
            raise ValueError("the passkey's algorithm is not one this broker offered")
        aaguid = str(cd.aaguid) if cd.aaguid else None
        return Registered(credential_id=bytes(cd.credential_id), public_key=cbor.encode(dict(cd.public_key)),
                          aaguid=aaguid, sign_count=int(auth_data.counter))

    # ------------------------------------------------------------- sign-in
    def auth_options(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The request options: no ``allowCredentials`` (the browser offers the owner's
        discoverable passkeys) and user verification required."""
        from fido2.webauthn import UserVerificationRequirement

        options, state = self.server.authenticate_begin(None, user_verification=UserVerificationRequirement.REQUIRED)
        return _json(options), _state(state)

    def auth_finish(self, state: dict[str, Any], passkeys: list[Any], credential: Any) -> tuple[bytes, int]:
        """Verify an assertion against the stored passkeys (``credential_id``, ``public_key``
        rows): the challenge, the origin, the RP id hash, user presence and verification,
        the signature. Returns (credential id, the sign count it sent); the caller applies
        ``sign_count_ok``."""
        from fido2 import cbor
        from fido2.cose import CoseKey
        from fido2.webauthn import AttestedCredentialData, UserVerificationRequirement

        cred = _credential(credential)
        creds = [AttestedCredentialData.create(b"\0" * 16, pk.credential_id, CoseKey.parse(cbor.decode(pk.public_key)))
                 for pk in passkeys]
        st = {"challenge": state["challenge"], "user_verification": UserVerificationRequirement(state["uv"])}
        matched = self.server.authenticate_complete(st, creds, cred)
        auth_data = cred["response"]["authenticatorData"]
        from fido2.utils import websafe_decode
        from fido2.webauthn import AuthenticatorData

        counter = AuthenticatorData(websafe_decode(auth_data)).counter
        return bytes(matched.credential_id), int(counter)


def _json(options: Any) -> dict[str, Any]:
    """A fido2 options object as plain JSON (bytes as base64url)."""
    return json.loads(json.dumps(dict(options)))


def _state(state: dict[str, Any]) -> dict[str, Any]:
    uv = state.get("user_verification")
    return {"challenge": state["challenge"], "uv": str(uv) if uv is not None else "preferred"}


def _credential(credential: Any) -> dict[str, Any]:
    """The browser's ``PublicKeyCredential.toJSON()`` as fido2 reads it; a short check
    before fido2's own parsing."""
    if not isinstance(credential, dict):
        raise ValueError("credential must be an object")
    if len(json.dumps(credential)) > MAX_CREDENTIAL:
        raise ValueError("credential too large")
    resp = credential.get("response")
    if not isinstance(credential.get("id"), str) or not isinstance(credential.get("rawId"), str) \
            or not isinstance(resp, dict):
        raise ValueError("credential is missing its id or response")
    # fido2 parses only what it knows; the browser may add fields (transports, extension results)
    return credential


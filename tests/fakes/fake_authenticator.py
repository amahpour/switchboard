"""A passkey in software, for the tests of a hosted broker's owner (DESIGN.md §31).

``SoftAuthenticator`` plays the browser and its authenticator: ``register`` answers the
broker's creation options with a real registration (an ES256 key made here, attestation
``none``, the flags a platform authenticator sets) and ``get`` answers request options with
a real assertion (a signature over the authenticator data and the client data hash, with a
sign count the test controls). Every field can be bent (another origin or RP id, no user
verification, a counter that doesn't grow) to check that the broker refuses it. The
credentials are JSON exactly as ``PublicKeyCredential.toJSON()`` gives them, which is what
``webauthn.js`` sends.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fido2.cose import ES256
from fido2.utils import websafe_decode, websafe_encode
from fido2.webauthn import AttestationObject, AttestedCredentialData, AuthenticatorData, CollectedClientData

FLAG = AuthenticatorData.FLAG
AAGUID = bytes.fromhex("0102030405060708090a0b0c0d0e0f10")


class Excluded(Exception):
    """The options excluded a credential this authenticator holds (a browser's InvalidStateError)."""


class SoftAuthenticator:
    def __init__(self, rp_id: str, origin: str, *, counts: bool = False):
        self.rp_id = rp_id
        self.origin = origin
        self.counts = counts  # a counting authenticator: each assertion's count is one more
        self.keys: dict[bytes, ec.EllipticCurvePrivateKey] = {}
        self.handles: dict[bytes, bytes] = {}  # credential id -> the user handle it was made for
        self.counter = 0
        self.last_id: bytes | None = None

    # ------------------------------------------------------------ registration
    def register(
        self,
        options: dict[str, Any],
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        uv: bool = True,
        up: bool = True,
        counter: int = 0,
    ) -> dict[str, Any]:
        pk = options["publicKey"]
        for d in pk.get("excludeCredentials") or []:
            if websafe_decode(d["id"]) in self.keys:
                raise Excluded("a credential of this authenticator is excluded")
        key = ec.generate_private_key(ec.SECP256R1())
        cred_id = os.urandom(32)
        self.keys[cred_id] = key
        self.handles[cred_id] = websafe_decode(pk["user"]["id"])
        self.last_id = cred_id
        cose = ES256.from_cryptography_key(key.public_key())
        acd = AttestedCredentialData.create(AAGUID, cred_id, cose)
        flags = FLAG.AT | (FLAG.UP if up else 0) | (FLAG.UV if uv else 0)
        auth = AuthenticatorData.create(
            hashlib.sha256((rp_id or self.rp_id).encode()).digest(), flags, counter, acd
        )
        att = AttestationObject.create("none", auth, {})
        cd = CollectedClientData.create("webauthn.create", pk["challenge"], origin or self.origin)
        return {
            "id": websafe_encode(cred_id),
            "rawId": websafe_encode(cred_id),
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": websafe_encode(cd),
                "attestationObject": websafe_encode(att),
                "transports": ["internal"],
            },
        }

    # ------------------------------------------------------------------ sign-in
    def get(
        self,
        options: dict[str, Any],
        *,
        credential_id: bytes | None = None,
        origin: str | None = None,
        rp_id: str | None = None,
        uv: bool = True,
        up: bool = True,
        counter: int | None = None,
        challenge: str | None = None,
    ) -> dict[str, Any]:
        pk = options["publicKey"]
        cid = credential_id if credential_id is not None else self.last_id
        assert cid is not None, "no credential registered"
        key = self.keys.get(cid)
        if counter is None:
            if self.counts:
                self.counter += 1
            counter = self.counter
        auth = AuthenticatorData.create(
            hashlib.sha256((rp_id or self.rp_id).encode()).digest(),
            (FLAG.UP if up else 0) | (FLAG.UV if uv else 0),
            counter,
        )
        cd = CollectedClientData.create("webauthn.get", challenge or pk["challenge"], origin or self.origin)
        if key is None:  # an unknown credential: a signature from a key the broker never saw
            key = ec.generate_private_key(ec.SECP256R1())
        sig = key.sign(bytes(auth) + cd.hash, ec.ECDSA(hashes.SHA256()))
        handle = self.handles.get(cid)
        return {
            "id": websafe_encode(cid),
            "rawId": websafe_encode(cid),
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": websafe_encode(cd),
                "authenticatorData": websafe_encode(auth),
                "signature": websafe_encode(sig),
                "userHandle": websafe_encode(handle) if handle is not None else None,
            },
        }

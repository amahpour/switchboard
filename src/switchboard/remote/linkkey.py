"""The keys, the signed handshake and the pairing codes of a link a machine dials
(DESIGN.md §31.7). The broker (``broker/machines.py``), the dialer (``remote/dialer.py``)
and ``switchboard remote join`` (``remote/join.py``) share them.

It does no I/O but the key files: no network, no spawn.

- **Keys** are Ed25519. A machine's is ``<its home>/link/id_ed25519``, the broker's
  ``<its home>/link/broker_ed25519``: PKCS#8 PEM, 0600, in a 0700 directory. A public key
  travels as its 32 raw bytes in base64url. A fingerprint is OpenSSH's, ``SHA256:…`` over
  the ``ssh-ed25519`` key blob, so ``ssh-keygen -lf`` on the same public key prints the same.
- **The handshake**, before M8's ``hello``::

      machine → broker  {"t":"auth","v":1,"name":…,"key":<pub>,"nm":<32-byte nonce>}
      broker  → machine {"t":"challenge","nb":<32-byte nonce>,"bkey":<broker pub>,
                         "sig":Sign_broker(broker_text(host, nm, nb))}
      machine → broker  {"t":"proof","sig":Sign_machine(machine_text(host, nb, nm))}
      broker  → machine {"t":"pending"}   (while the machine awaits approval)
      broker  → machine {"t":"approved"}  (then M8's hello, welcome, …)

  or ``{"t":"refuse","why":…,"message":…}`` at any step. Each signed text names its role,
  the public URL's host and both nonces, so a signature can't be used for the other role,
  for another broker, or later.
- **Pairing codes** are 12 characters of Crockford base32 (60 bits), shown
  ``XXXX-XXXX-XXXX``. Typed back, case, dashes and blanks don't matter, and ``I``, ``L``
  and ``O`` read as ``1``, ``1`` and ``0``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from switchboard.models import valid_host
from switchboard.paths import ensure_private_dir

LINK_VERSION = 1
LINK_PATH = "/link"
PAIR_PATH = "/link/pair"
HANDSHAKE_TIMEOUT_S = 5.0  # each step of the handshake
HANDSHAKE_MAX = 4096  # a handshake frame, at most (M8's MAX_FRAME applies once it is done)
NONCE_BYTES = 32
MACHINE_KEY = "id_ed25519"
BROKER_KEY = "broker_ed25519"
# why a broker ends a machine's connection for good (the dialer stops, and says how to pair
# again), and why it ends one that may come back
FINAL_REFUSALS = frozenset({"removed", "unknown"})
REFUSALS = FINAL_REFUSALS | frozenset({"replaced", "proof", "protocol", "shutdown", "busy"})

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_CHARS = 12
_CODE_FIX = str.maketrans("ILO", "110")


class KeyFileError(Exception):
    """A key file that is missing, not private, or not an Ed25519 key."""


class HandshakeError(ValueError):
    """A handshake frame that isn't valid: ``code`` names why (no frame content in it)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------- encoding
def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64u(text: Any, size: int | None = None) -> bytes:
    """Strict base64url (no padding, no other characters); exactly ``size`` bytes if given."""
    if not isinstance(text, str) or len(text) > 256 or "=" in text:
        raise ValueError("not base64url")
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("not base64url") from None
    if b64u(data) != text:
        raise ValueError("not canonical base64url")
    if size is not None and len(data) != size:
        raise ValueError(f"not {size} bytes")
    return data


# ----------------------------------------------------------------------- keys
def pub_raw(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def fingerprint(pub: bytes) -> str:
    """OpenSSH's fingerprint of an Ed25519 public key: ``SHA256:<base64>`` over its
    ``ssh-ed25519`` blob."""
    if len(pub) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + pub
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def make_key(path: Path) -> Ed25519PrivateKey:
    """A new key at ``path`` (0600, its directory 0700), replacing any key there."""
    ensure_private_dir(path.parent)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return key


def load_key(path: Path) -> Ed25519PrivateKey:
    """The key at ``path``: a regular file of this user that nobody else can read."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise KeyFileError(f"{path} is missing") from None
    except OSError as e:
        raise KeyFileError(f"{path}: {e.strerror or e}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise KeyFileError(f"{path} must be a private file of yours (0600)")
        data = os.read(fd, 16 * 1024)
    finally:
        os.close(fd)
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        raise KeyFileError(f"{path} is not a key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyFileError(f"{path} is not an Ed25519 key")
    return key


def load_or_make_key(path: Path) -> Ed25519PrivateKey:
    """The broker's own key: made at the first start, the same one from then on."""
    if not os.path.lexists(path):
        return make_key(path)
    return load_key(path)


# ------------------------------------------------------------------ signatures
def signed_text(role: str, host: str, first: bytes, second: bytes) -> bytes:
    """What a side signs: its role, the public URL's host (as browsers send it in Host) and
    the two nonces, the other side's first."""
    if role not in ("broker", "machine"):
        raise ValueError("role")
    if len(first) != NONCE_BYTES or len(second) != NONCE_BYTES:
        raise ValueError("nonces are 32 bytes")
    return b"sb-link/1/" + role.encode("ascii") + b"\n" + host.encode("ascii") + b"\n" + first + second


def sign(key: Ed25519PrivateKey, role: str, host: str, first: bytes, second: bytes) -> str:
    return b64u(key.sign(signed_text(role, host, first, second)))


def verify(pub: bytes, sig: str, role: str, host: str, first: bytes, second: bytes) -> bool:
    try:
        raw = unb64u(sig, 64)
        Ed25519PublicKey.from_public_bytes(pub).verify(raw, signed_text(role, host, first, second))
    except (ValueError, InvalidSignature):
        return False
    return True


# ------------------------------------------------------------ handshake frames
def check_frame(obj: Any, t: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(obj, dict) or obj.get("t") != t:
        raise HandshakeError("bad_type")
    if set(obj) != keys | {"t"}:
        raise HandshakeError("bad_fields")
    return obj


def check_auth(obj: Any) -> tuple[str, bytes, bytes]:
    """(name, key, nonce) of an ``auth`` frame."""
    f = check_frame(obj, "auth", {"v", "name", "key", "nm"})
    if f["v"] != LINK_VERSION:
        raise HandshakeError("bad_version")
    if not isinstance(f["name"], str) or not valid_host(f["name"]):
        raise HandshakeError("bad_name")
    try:
        return f["name"], unb64u(f["key"], 32), unb64u(f["nm"], NONCE_BYTES)
    except ValueError:
        raise HandshakeError("bad_key") from None


def check_challenge(obj: Any) -> tuple[bytes, bytes, str]:
    """(nonce, broker key, signature) of a ``challenge`` frame."""
    f = check_frame(obj, "challenge", {"nb", "bkey", "sig"})
    try:
        return unb64u(f["nb"], NONCE_BYTES), unb64u(f["bkey"], 32), b64u(unb64u(f["sig"], 64))
    except ValueError:
        raise HandshakeError("bad_challenge") from None


def check_proof(obj: Any) -> str:
    f = check_frame(obj, "proof", {"sig"})
    try:
        return b64u(unb64u(f["sig"], 64))
    except ValueError:
        raise HandshakeError("bad_proof") from None


def refuse(why: str, message: str) -> dict[str, Any]:
    return {"t": "refuse", "why": why, "message": message[:300]}


def check_refuse(obj: Any) -> tuple[str, str]:
    f = check_frame(obj, "refuse", {"why", "message"})
    if f["why"] not in REFUSALS or not isinstance(f["message"], str):
        raise HandshakeError("bad_refuse")
    return f["why"], f["message"][:300]


# ------------------------------------------------------------- pairing codes
def make_code() -> str:
    raw = "".join(secrets.choice(CROCKFORD) for _ in range(CODE_CHARS))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_code(text: Any) -> str | None:
    """A code as typed, in its canonical 12 characters, or None if it can't be one."""
    if not isinstance(text, str) or len(text) > 40:
        return None
    s = "".join(text.split()).replace("-", "").upper().translate(_CODE_FIX)
    if len(s) != CODE_CHARS or any(c not in CROCKFORD for c in s):
        return None
    return s


def code_hash(canonical: str) -> bytes:
    return hashlib.sha256(canonical.encode("ascii")).digest()
